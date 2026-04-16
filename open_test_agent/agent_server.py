"""
本地 Agent HTTP 服务器
监听 localhost:7357，执行需要本地设备的节点（appUiAction / ADB）
"""
import asyncio
import json
import time
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

AGENT_PORT = 7357
_MAX_LOGS = 200   # 最多保留最近 200 条

# ---------------------------------------------------------------------------
# 日志缓冲区
# ---------------------------------------------------------------------------

_logs: deque[dict] = deque(maxlen=_MAX_LOGS)
_log_listeners: list[asyncio.Queue] = []


def _log(level: str, node_id: str | None, message: str, extra: dict | None = None):
    entry = {
        "ts": datetime.now().strftime("%H:%M:%S"),
        "level": level,   # info / success / error
        "node_id": node_id,
        "message": message,
        **(extra or {}),
    }
    _logs.append(entry)
    for q in _log_listeners:
        q.put_nowait(entry)


# ---------------------------------------------------------------------------
# 模型
# ---------------------------------------------------------------------------

class ExecuteRequest(BaseModel):
    node_id: str
    node_data: dict


class ExecuteResponse(BaseModel):
    success: bool
    message: str
    duration: float   # seconds


# ---------------------------------------------------------------------------
# 应用
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    from open_test_agent.drivers.adb import check_adb
    devices = check_adb()
    _log("info", None, f"Agent 启动，已连接设备: {', '.join(devices) if devices else '无'}")
    yield


app = FastAPI(title="open-test-agent", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["POST", "GET"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    from open_test_agent.drivers.adb import check_adb
    devices = check_adb()
    return {"status": "ok", "devices": devices}


@app.post("/execute", response_model=ExecuteResponse)
async def execute_node(body: ExecuteRequest):
    """执行单个节点，返回执行结果。"""
    t0 = time.perf_counter()
    node_type = body.node_data.get("_node_type", "appUiAction")
    action = body.node_data.get("action", "")
    label = body.node_data.get("label", body.node_id)

    _log("info", body.node_id, f"▶ {label}  action={action}  type={node_type}",
         {"action": action, "node_type": node_type})

    try:
        if node_type == "appUiAction":
            from open_test_agent.drivers.adb import run_app_ui_action
            success, message = await run_app_ui_action(body.node_data)
        else:
            success, message = False, f"Agent 不支持的节点类型: {node_type}"
    except Exception as exc:
        success, message = False, str(exc)

    duration = time.perf_counter() - t0
    level = "success" if success else "error"
    icon = "✓" if success else "✗"
    _log(level, body.node_id, f"{icon} {message}  ({duration*1000:.0f}ms)",
         {"success": success, "duration_ms": round(duration * 1000)})

    return ExecuteResponse(success=success, message=message, duration=duration)


@app.get("/logs")
async def get_logs(limit: int = 100):
    """返回最近 limit 条日志。"""
    entries = list(_logs)
    return entries[-limit:]


@app.get("/logs/stream")
async def stream_logs():
    """SSE 实时推送日志（先回放历史，再推新条目）。"""
    q: asyncio.Queue = asyncio.Queue()
    _log_listeners.append(q)

    async def _gen():
        try:
            # 先推历史
            for entry in list(_logs):
                yield f"data: {json.dumps(entry, ensure_ascii=False)}\n\n"
            # 再实时推
            while True:
                try:
                    entry = await asyncio.wait_for(q.get(), timeout=30)
                    yield f"data: {json.dumps(entry, ensure_ascii=False)}\n\n"
                except asyncio.TimeoutError:
                    yield "data: {\"type\":\"heartbeat\"}\n\n"
        finally:
            _log_listeners.remove(q)

    return StreamingResponse(
        _gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------------------
# 启动
# ---------------------------------------------------------------------------

def start_server():
    """启动本地 Agent 服务（阻塞）。"""
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=AGENT_PORT, log_level="warning")
