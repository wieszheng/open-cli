"""
本地 Agent HTTP 服务器
监听 localhost:7357，执行需要本地设备的节点（App UI 操作 / ADB / HDC）

device_type 字段决定使用哪个驱动：
  - "android"（默认）→ adb.py（adbutils）
  - "harmony"         → hdc.py（hdc CLI + uitest）
"""
import asyncio
import base64
import json
import time
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, Query
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
    screenshot: str | None = None  # base64 PNG，无截图时为 null


# ---------------------------------------------------------------------------
# 应用
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    from open_test_agent.drivers.adb import check_adb
    from open_test_agent.drivers.hdc import check_hdc
    android_devices = check_adb()
    harmony_devices = check_hdc()
    parts = []
    if android_devices:
        parts.append(f"Android: {', '.join(android_devices)}")
    if harmony_devices:
        parts.append(f"HarmonyOS: {', '.join(harmony_devices)}")
    _log("info", None, f"Agent 启动，已连接设备: {'; '.join(parts) if parts else '无'}")
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
    from open_test_agent.drivers.hdc import check_hdc
    return {
        "status": "ok",
        "devices": {
            "android": check_adb(),
            "harmony": check_hdc(),
        }
    }


@app.get("/screenshot")
async def screenshot(serial: str | None = Query(None), device_type: str = Query("android")):
    """截取当前设备屏幕，返回 base64 PNG + 分辨率。device_type: android | harmony"""
    try:
        if device_type == "harmony":
            from open_test_agent.drivers.hdc import capture_screenshot
        else:
            from open_test_agent.drivers.adb import capture_screenshot
        img_bytes, width, height = await capture_screenshot(serial or None)
        b64 = base64.b64encode(img_bytes).decode()
        return {
            "image": f"data:image/png;base64,{b64}",
            "width": width,
            "height": height,
        }
    except Exception as exc:
        from fastapi import HTTPException
        raise HTTPException(status_code=500, detail=str(exc))


# App UI 操作节点类型集合（与前端 nodes.tsx 中 APP_UI_NODE_TYPES 保持一致）
_APP_UI_NODE_TYPES = {
    "appUiAction",       # 旧类型，向后兼容
    "appLaunchApp", "appClick", "appLongPress", "appDoubleClick",
    "appType", "appClearText", "appSwipe", "appTapXy",
    "appWaitElement", "appGetText", "appScreenshot", "appPressKey",
}


@app.post("/execute", response_model=ExecuteResponse)
async def execute_node(body: ExecuteRequest):
    """执行单个节点，返回执行结果。"""
    t0 = time.perf_counter()
    node_type = body.node_data.get("_node_type", "appLaunchApp")
    action = body.node_data.get("action", "")
    label = body.node_data.get("label", body.node_id)

    _log("info", body.node_id, f"▶ {label}  action={action}  type={node_type}",
         {"action": action, "node_type": node_type})

    try:
        if node_type in _APP_UI_NODE_TYPES:
            device_type = body.node_data.get("device_type", "android")
            if device_type == "harmony":
                from open_test_agent.drivers.hdc import run_app_ui_action
            else:
                from open_test_agent.drivers.adb import run_app_ui_action
            success, message, shot_bytes = await run_app_ui_action(body.node_data)
        else:
            success, message, shot_bytes = False, f"Agent 不支持的节点类型: {node_type}", None
    except Exception as exc:
        success, message, shot_bytes = False, str(exc), None

    screenshot: str | None = None
    if shot_bytes:
        import base64 as _b64
        screenshot = "data:image/png;base64," + _b64.b64encode(shot_bytes).decode()

    duration = time.perf_counter() - t0
    level = "success" if success else "error"
    icon = "✓" if success else "✗"
    _log(level, body.node_id, f"{icon} {message}  ({duration*1000:.0f}ms)",
         {"success": success, "duration_ms": round(duration * 1000)})

    return ExecuteResponse(success=success, message=message, duration=duration, screenshot=screenshot)


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
