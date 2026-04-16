"""
工作流执行引擎
使用 graphlib.TopologicalSorter 按拓扑序执行节点
"""
import re
import time
import asyncio
import inspect
from graphlib import TopologicalSorter, CycleError
from typing import Any, Callable

console_print = print  # 避免循环依赖

# 变量占位符正则：{{varName}}
_VAR_RE = re.compile(r"\{\{(\w+)\}\}")


def _render(value: Any, ctx: dict) -> Any:
    """将字符串中的 {{var}} 替换为上下文变量。"""
    if isinstance(value, str):
        return _VAR_RE.sub(lambda m: str(ctx.get(m.group(1), m.group(0))), value)
    return value


def _build_graph(nodes: list, edges: list) -> dict[str, set]:
    """根据 edges 构建依赖图 {node_id: {dep_ids...}}。"""
    node_ids = {n["id"] for n in nodes}
    deps: dict[str, set] = {n["id"]: set() for n in nodes}
    for e in edges:
        src, tgt = e.get("source"), e.get("target")
        if src in node_ids and tgt in node_ids:
            deps[tgt].add(src)
    return deps


async def _call(fn, *args):
    """统一调用同步或异步回调。"""
    if fn is None:
        return
    result = fn(*args)
    if inspect.isawaitable(result):
        await result


async def execute_flow(
    flow: dict,
    *,
    on_node_start: Callable | None = None,
    on_node_done: Callable | None = None,
) -> dict:
    """
    执行工作流。

    Parameters
    ----------
    flow : dict  {"nodes": [...], "edges": [...]}
    on_node_start : callable(node_id, label)            同步或异步均可
    on_node_done  : callable(node_id, label, success, message, duration_s)

    Returns
    -------
    {"total": int, "passed": int, "failed": int}
    """
    nodes: list = flow.get("nodes", [])
    edges: list = flow.get("edges", [])

    if not nodes:
        return {"total": 0, "passed": 0, "failed": 0}

    node_map = {n["id"]: n for n in nodes}
    deps = _build_graph(nodes, edges)

    try:
        sorter = TopologicalSorter(deps)
        sorter.prepare()
    except CycleError as e:
        raise RuntimeError(f"工作流存在循环依赖: {e}") from e

    ctx: dict = {}          # 运行时变量上下文
    results: dict = {}      # node_id -> {"success": bool, "message": str}
    total = len(nodes)
    passed = 0

    while sorter.is_active():
        ready = list(sorter.get_ready())
        # 并发执行所有就绪节点（当前为顺序执行，后续可改 gather）
        for nid in ready:
            node = node_map[nid]
            data = node.get("data", {})
            label = data.get("label") or nid
            t0 = time.perf_counter()

            await _call(on_node_start, nid, label)

            success, message = await _execute_node(node, ctx)
            duration = time.perf_counter() - t0

            results[nid] = {"success": success, "message": message}
            if success:
                passed += 1

            await _call(on_node_done, nid, label, success, message, duration)

            sorter.done(nid)

        # 让出事件循环
        await asyncio.sleep(0)

    return {"total": total, "passed": passed, "failed": total - passed}


# ---------------------------------------------------------------------------
# 各类型节点执行（Stub 实现，可按需替换为真实 Driver）
# ---------------------------------------------------------------------------

async def _execute_node(node: dict, ctx: dict) -> tuple[bool, str]:
    """分发到具体节点类型处理器。"""
    ntype = node.get("type", "")
    data = node.get("data", {})

    handlers = {
        "httpRequest":  _run_http,
        "webUiAction":  _run_web_ui,
        "appUiAction":  _run_app_ui,
        "sqlQuery":     _run_sql,
        "assertion":    _run_assertion,
        "extract":      _run_extract,
        "script":       _run_script,
        "wait":         _run_wait,
        "condition":    _run_condition,
    }

    handler = handlers.get(ntype)
    if handler is None:
        return False, f"未知节点类型: {ntype}"

    try:
        return await handler(data, ctx)
    except Exception as exc:  # pylint: disable=broad-except
        return False, str(exc)


async def _run_http(data: dict, ctx: dict) -> tuple[bool, str]:
    """HTTP 请求节点（使用 httpx）。"""
    import httpx

    method = data.get("method", "GET").upper()
    url = _render(data.get("url", ""), ctx)
    if not url:
        return False, "URL 不能为空"

    headers_raw = data.get("headers", "")
    body_raw = data.get("body", "")
    timeout = float(data.get("timeout", 30000)) / 1000

    import json as _json
    try:
        headers = _json.loads(_render(headers_raw, ctx)) if headers_raw else {}
    except Exception:
        headers = {}
    try:
        body = _json.loads(_render(body_raw, ctx)) if body_raw else None
    except Exception:
        body = _render(body_raw, ctx) or None

    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.request(method, url, headers=headers, json=body)

    ctx["_last_response_status"] = resp.status_code
    ctx["_last_response_body"] = resp.text
    try:
        ctx["_last_response_json"] = resp.json()
    except Exception:
        ctx["_last_response_json"] = None

    return True, f"{resp.status_code} {resp.reason_phrase}"


async def _run_web_ui(data: dict, ctx: dict) -> tuple[bool, str]:
    """Web UI 操作节点（Stub：仅打印，不真实操作）。"""
    action = data.get("action", "click")
    await asyncio.sleep(0.05)   # 模拟耗时
    return True, f"[stub] webUI.{action} OK"


async def _run_app_ui(data: dict, ctx: dict) -> tuple[bool, str]:
    """App UI 操作节点（Stub）。"""
    action = data.get("action", "click")
    await asyncio.sleep(0.05)
    return True, f"[stub] appUI.{action} OK"


async def _run_sql(data: dict, ctx: dict) -> tuple[bool, str]:
    """SQL 查询节点（Stub）。"""
    query = data.get("query", "")
    extract_var = data.get("extractVar", "")
    await asyncio.sleep(0.02)
    if extract_var:
        ctx[extract_var] = "[stub sql result]"
    return True, f"[stub] SQL OK — '{query[:40]}'"


async def _run_assertion(data: dict, ctx: dict) -> tuple[bool, str]:
    """断言节点 — 简易实现。"""
    assert_type = data.get("assertType", "status_code")
    expression = _render(data.get("expression", ""), ctx)
    expected = _render(str(data.get("expected", "")), ctx)

    if assert_type == "status_code":
        actual = str(ctx.get("_last_response_status", ""))
        if actual == expected:
            return True, f"status_code {actual} == {expected}"
        return False, f"AssertionError: expected status {expected} but got {actual}"

    if assert_type in ("contains", "equals"):
        body = ctx.get("_last_response_body", "")
        if assert_type == "contains" and expected in body:
            return True, f"body contains '{expected}'"
        if assert_type == "equals" and body == expected:
            return True, "body equals expected"
        return False, f"AssertionError: {assert_type} failed (expression={expression})"

    # 其他类型 stub 通过
    return True, f"[stub] assertion.{assert_type} OK"


async def _run_extract(data: dict, ctx: dict) -> tuple[bool, str]:
    """变量提取节点。"""
    import json as _json
    import jsonpath_ng as _jng  # type: ignore[import]

    source = data.get("source", "json_path")
    expression = data.get("expression", "")
    var_name = data.get("varName", "")

    if not var_name:
        return False, "varName 不能为空"

    if source == "json_path":
        resp_json = ctx.get("_last_response_json")
        if resp_json is None:
            return False, "上一步没有 JSON 响应"
        try:
            matches = [m.value for m in _jng.parse(expression).find(resp_json)]
            ctx[var_name] = matches[0] if len(matches) == 1 else matches
            return True, f"extracted {var_name}={ctx[var_name]!r}"
        except Exception as exc:
            return False, str(exc)

    if source == "header":
        # 头部提取 stub
        ctx[var_name] = "[stub header]"
        return True, f"[stub] header extracted to {var_name}"

    ctx[var_name] = "[stub extracted]"
    return True, f"[stub] {source} extracted to {var_name}"


async def _run_script(data: dict, ctx: dict) -> tuple[bool, str]:
    """脚本节点（Stub — 不执行真实代码）。"""
    lang = data.get("language", "python")
    code = data.get("code", "")
    await asyncio.sleep(0.05)
    return True, f"[stub] {lang} script OK ({len(code)} chars)"


async def _run_wait(data: dict, ctx: dict) -> tuple[bool, str]:
    """等待节点。"""
    seconds = float(data.get("seconds", 2))
    await asyncio.sleep(seconds)
    return True, f"waited {seconds}s"


async def _run_condition(data: dict, ctx: dict) -> tuple[bool, str]:
    """条件节点（Stub — 始终走 True 分支）。"""
    expression = _render(data.get("expression", "true"), ctx)
    await asyncio.sleep(0)
    return True, f"[stub] condition '{expression[:40]}' → True"
