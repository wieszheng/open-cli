"""
open-test-agent CLI 入口
用法:
  open-test run --case-id 1                              # 从服务器拉取流程并在本地执行
  open-test run --file flow.json                         # 直接读取本地 JSON 文件执行
  open-test run --case-id 1 --job-id <uuid>              # 绑定浏览器任务，推送 SSE 事件
  open-test run --case-id 1 --server http://...          # 指定服务器地址
"""
import asyncio
import json
import sys
from pathlib import Path

import click
import httpx
from rich.console import Console
from rich.live import Live
from rich.table import Table
from rich.text import Text
from rich import box

from open_test_agent.executor import execute_flow

console = Console()

DEFAULT_SERVER = "http://localhost:8000"


def _fetch_flow(server: str, case_id: int) -> dict:
    """从服务器拉取指定测试用例的工作流。"""
    url = f"{server}/workflows/{case_id}"
    try:
        with httpx.Client(timeout=10) as client:
            resp = client.get(url)
            resp.raise_for_status()
            data = resp.json()
            if data is None:
                console.print(f"[red]✗[/] 测试用例 {case_id} 尚未编排工作流")
                sys.exit(1)
            return data
    except httpx.ConnectError:
        console.print(f"[red]✗[/] 无法连接服务器 {server}，请确认后端已启动")
        sys.exit(1)
    except httpx.HTTPStatusError as e:
        console.print(f"[red]✗[/] 服务器返回错误: {e.response.status_code}")
        sys.exit(1)


async def _push(server: str, job_id: str, event: dict):
    """异步推送单个事件到服务器（静默失败）。"""
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            await client.post(f"{server}/run-jobs/{job_id}/events", json=event)
    except Exception:  # pylint: disable=broad-except
        pass  # CLI 继续执行，不因推送失败而中断


def _make_table(rows: list[dict]) -> Table:
    """构建执行状态表格。"""
    table = Table(box=box.ROUNDED, show_header=True, header_style="bold dim")
    table.add_column("步骤", style="dim", width=28)
    table.add_column("状态", width=10)
    table.add_column("耗时", width=8, justify="right")
    table.add_column("信息")

    status_style = {
        "pending":  "dim",
        "running":  "yellow",
        "success":  "green",
        "error":    "red",
    }

    for r in rows:
        st = r["status"]
        style = status_style.get(st, "")
        icon = {"pending": "·", "running": "⟳", "success": "✓", "error": "✗"}.get(st, "?")
        dur = f"{r['duration']:.2f}s" if r.get("duration") is not None else ""
        table.add_row(
            Text(r["label"], overflow="fold"),
            Text(f"{icon} {st}", style=style),
            dur,
            Text(r.get("message", ""), overflow="fold"),
        )
    return table


@click.group()
def cli():
    """open-test-agent — 本地工作流执行工具"""


@cli.command()
@click.option("--case-id", "-c", type=int, default=None, help="测试用例 ID（从服务器获取工作流）")
@click.option("--file", "-f", "flow_file", type=click.Path(exists=True), default=None, help="本地 flow JSON 文件路径")
@click.option("--server", "-s", default=DEFAULT_SERVER, show_default=True, help="后端服务器地址")
@click.option("--job-id", "-j", default=None, help="绑定浏览器 RunJob ID，执行事件实时推送到服务器")
def run(case_id: int | None, flow_file: str | None, server: str, job_id: str | None):
    """执行工作流。"""
    if case_id is None and flow_file is None:
        console.print("[red]✗[/] 请指定 --case-id 或 --file")
        sys.exit(1)

    # 加载流程数据
    if flow_file:
        console.print(f"[dim]从文件加载:[/] {flow_file}")
        flow = json.loads(Path(flow_file).read_text(encoding="utf-8"))
    else:
        console.print(f"[dim]从服务器加载 case_id={case_id}:[/] {server}")
        flow = _fetch_flow(server, case_id)

    nodes = flow.get("nodes", [])
    if not nodes:
        console.print("[yellow]⚠[/] 工作流为空，没有可执行的节点")
        sys.exit(0)

    if job_id:
        console.print(f"[dim]绑定 job_id:[/] {job_id}")

    console.print(f"\n[bold]工作流执行[/] — [cyan]{len(nodes)}[/] 个节点\n")

    # 构建初始行列表
    rows: list[dict] = [
        {
            "id": n["id"],
            "label": (n.get("data") or {}).get("label") or n["id"],
            "status": "pending",
            "duration": None,
            "message": "",
        }
        for n in nodes
    ]
    id_to_row = {r["id"]: r for r in rows}

    async def _run():
        with Live(_make_table(rows), console=console, refresh_per_second=8) as live:

            async def on_start(nid: str, label: str):
                id_to_row[nid]["status"] = "running"
                live.update(_make_table(rows))
                if job_id:
                    await _push(server, job_id, {"type": "node_start", "node_id": nid, "label": label})

            async def on_done(nid: str, label: str, success: bool, message: str, duration: float):
                r = id_to_row[nid]
                r["status"] = "success" if success else "error"
                r["message"] = message
                r["duration"] = duration
                live.update(_make_table(rows))
                if job_id:
                    await _push(server, job_id, {
                        "type": "node_done",
                        "node_id": nid,
                        "label": label,
                        "success": success,
                        "message": message,
                        "duration": duration,
                    })

            result = await execute_flow(flow, on_node_start=on_start, on_node_done=on_done)

            # 推送完成事件
            if job_id:
                await _push(server, job_id, {
                    "type": "complete",
                    "node_id": None,
                    "success": result["failed"] == 0,
                    "message": f"{result['passed']}/{result['total']} 通过",
                    "duration": None,
                })

        return result

    result = asyncio.run(_run())

    total, passed, failed = result["total"], result["passed"], result["failed"]
    console.print()
    if failed == 0:
        console.print(f"[bold green]✓ 全部通过[/] {passed}/{total}")
    else:
        console.print(f"[bold red]✗ 存在失败[/] {passed} 通过 / {failed} 失败 / {total} 总计")

    sys.exit(0 if failed == 0 else 1)


def main():
    cli()


# ---------------------------------------------------------------------------
# agent 子命令组
# ---------------------------------------------------------------------------

@click.group()
def agent():
    """管理本地 Agent 系统服务（用于 ADB 设备执行）"""


cli.add_command(agent)


@agent.command("install")
def agent_install():
    """安装并启动本地 Agent 系统服务（开机自启）"""
    from open_test_agent.install import install
    msg = install()
    console.print(f"[green]✓[/] {msg}")


@agent.command("uninstall")
def agent_uninstall():
    """卸载本地 Agent 系统服务"""
    from open_test_agent.install import uninstall
    msg = uninstall()
    console.print(f"[yellow]·[/] {msg}")


@agent.command("status")
def agent_status():
    """查看本地 Agent 运行状态"""
    from open_test_agent.install import status
    msg = status()
    if "运行中" in msg:
        console.print(f"[green]● 运行中[/] — {msg}")
    else:
        console.print(f"[red]○ 未运行[/] — 使用 [bold]open-test agent install[/] 安装")


@agent.command("start")
def agent_start():
    """在前台启动本地 Agent（用于测试/调试）"""
    from open_test_agent.agent_server import start_server
    console.print(f"[dim]启动本地 Agent，监听 localhost:7357 ...[/]")
    start_server()



if __name__ == "__main__":
    main()
