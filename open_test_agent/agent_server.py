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
    """截取当前设备屏幕，返回 base64 图片 + 分辨率。device_type: android | harmony"""
    try:
        if device_type == "harmony":
            from open_test_agent.drivers.hdc import capture_screenshot
        else:
            from open_test_agent.drivers.adb import capture_screenshot
        img_bytes, width, height = await capture_screenshot(serial or None)
        b64 = base64.b64encode(img_bytes).decode()
        mime = "image/jpeg" if device_type == "harmony" else "image/png"
        return {
            "image": f"data:{mime};base64,{b64}",
            "width": width,
            "height": height,
        }
    except Exception as exc:
        from fastapi import HTTPException
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/layout")
async def layout(serial: str | None = Query(None), device_type: str = Query("android")):
    """
    获取 UI 布局树（控件树），叠加在截图上供 ScreenshotPicker DOM 模式使用。
    返回 {image, width, height, elements: [{id, text, type, description, selector, x1,y1,x2,y2}]}
    """
    try:
        if device_type == "harmony":
            from open_test_agent.drivers.hdc import capture_screenshot, _dump_layout
        else:
            from open_test_agent.drivers.adb import capture_screenshot, _dump_hierarchy

        img_bytes, width, height = await capture_screenshot(serial or None)
        b64 = base64.b64encode(img_bytes).decode()
        mime = "image/jpeg" if device_type == "harmony" else "image/png"

        elements: list[dict] = []

        if device_type == "harmony":
            import asyncio as _asyncio, re as _re
            loop = _asyncio.get_event_loop()
            tree = await loop.run_in_executor(None, _dump_layout, serial or None)

            def _walk(node: dict):
                attrs = node.get("attributes", {})
                bounds = attrs.get("bounds", "")
                m = _re.match(r'\[(\d+),(\d+)\]\[(\d+),(\d+)\]', bounds)
                if m:
                    x1, y1, x2, y2 = map(int, m.groups())
                    if x2 > x1 and y2 > y1:
                        node_id   = attrs.get("id") or attrs.get("key") or ""
                        text      = attrs.get("text") or ""
                        desc      = attrs.get("description") or ""
                        node_type = attrs.get("type") or ""
                        # 计算最优 selector
                        if node_id:
                            sel = f"#{node_id}"
                        elif text:
                            sel = text
                        elif desc:
                            sel = desc
                        elif node_type:
                            sel = node_type.split(".")[-1]
                        else:
                            sel = ""
                        elements.append({
                            "id": node_id, "text": text,
                            "type": node_type.split(".")[-1] if node_type else "",
                            "description": desc,
                            "selector": sel,
                            "x1": x1, "y1": y1, "x2": x2, "y2": y2,
                        })
                for child in node.get("children", []):
                    _walk(child)
            _walk(tree)

        else:
            import asyncio as _asyncio, re as _re
            loop = _asyncio.get_event_loop()
            tree = await loop.run_in_executor(None, _dump_hierarchy, serial or None)

            def _walk_android(node: dict):
                bounds_str = node.get("@bounds", "")
                m = _re.match(r'\[(\d+),(\d+)\]\[(\d+),(\d+)\]', bounds_str)
                if m:
                    x1, y1, x2, y2 = map(int, m.groups())
                    if x2 > x1 and y2 > y1:
                        res_id = node.get("@resource-id", "")
                        text   = node.get("@text", "")
                        desc   = node.get("@content-desc", "")
                        cls    = node.get("@class", "")
                        short_cls = cls.split(".")[-1] if cls else ""
                        if res_id:
                            sel = res_id
                        elif text:
                            sel = text
                        elif desc:
                            sel = desc
                        elif short_cls:
                            sel = short_cls
                        else:
                            sel = ""
                        elements.append({
                            "id": res_id, "text": text,
                            "type": short_cls, "description": desc,
                            "selector": sel,
                            "x1": x1, "y1": y1, "x2": x2, "y2": y2,
                        })
                for child in node.get("node", []):
                    _walk_android(child if isinstance(child, dict) else {})
            _walk_android(tree)

        return {
            "image": f"data:{mime};base64,{b64}",
            "width": width,
            "height": height,
            "elements": elements,
        }
    except Exception as exc:
        from fastapi import HTTPException
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/ocr")
async def ocr_detect(serial: str | None = Query(None), device_type: str = Query("android")):
    """
    截图 + OCR 文字识别，返回 {image, width, height, regions: [{text, x1,y1,x2,y2, confidence}]}
    依赖：paddleocr（可选，未安装时返回空 regions）
    """
    try:
        if device_type == "harmony":
            from open_test_agent.drivers.hdc import capture_screenshot
        else:
            from open_test_agent.drivers.adb import capture_screenshot

        img_bytes, width, height = await capture_screenshot(serial or None)
        b64 = base64.b64encode(img_bytes).decode()
        mime = "image/jpeg" if device_type == "harmony" else "image/png"
        regions: list[dict] = []

        try:
            import asyncio as _asyncio
            import numpy as _np
            import cv2 as _cv2
            from paddleocr import PaddleOCR

            loop = _asyncio.get_event_loop()

            def _run_ocr():
                nparr = _np.frombuffer(img_bytes, _np.uint8)
                img = _cv2.imdecode(nparr, _cv2.IMREAD_COLOR)
                ocr = PaddleOCR(use_angle_cls=False, lang="ch", show_log=False)
                result = ocr.ocr(img, cls=False)
                out = []
                for line in (result or []):
                    for item in (line or []):
                        if not item or len(item) < 2:
                            continue
                        box, (text, conf) = item
                        xs = [p[0] for p in box]
                        ys = [p[1] for p in box]
                        out.append({
                            "text": text,
                            "confidence": round(float(conf), 3),
                            "x1": int(min(xs)), "y1": int(min(ys)),
                            "x2": int(max(xs)), "y2": int(max(ys)),
                        })
                return out

            regions = await loop.run_in_executor(None, _run_ocr)
        except ImportError:
            pass  # paddleocr 未安装时返回空列表

        return {
            "image": f"data:{mime};base64,{b64}",
            "width": width,
            "height": height,
            "regions": regions,
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

# 不需要元素定位的 action（直接执行）
_NO_LOCATE_ACTIONS = {"launch_app", "swipe", "screenshot", "press_key", "tap_xy"}

# wait_element 轮询参数
_WAIT_ELEMENT_TIMEOUT  = 15   # 秒
_WAIT_ELEMENT_INTERVAL = 0.8  # 秒


@app.post("/execute", response_model=ExecuteResponse)
async def execute_node(body: ExecuteRequest):
    """
    执行单个 App UI 节点。

    混合定位流程：
      1. 无需定位的 action（launch_app/swipe/tap_xy/screenshot/press_key）→ 直接执行
      2. 需要定位的 action → locator.locate_element() 按策略依次尝试：
            selector（结构树）→ image（OpenCV）→ ocr（PaddleOCR）→ ai_vision（LLM）
      3. wait_element → 在超时内轮询 locate_element()
      4. 定位成功后调用驱动层 run_action_at() 执行具体动作
      5. 执行完成后截图（失败不影响主流程），统一返回
    """
    t0 = time.perf_counter()
    node_type   = body.node_data.get("_node_type", "appLaunchApp")
    device_type = body.node_data.get("device_type", "android")
    serial      = body.node_data.get("device_serial") or None
    action      = body.node_data.get("action", "")
    label       = body.node_data.get("label", body.node_id)

    _log("info", body.node_id,
         f"▶ {label}  action={action}  device={device_type}",
         {"action": action, "node_type": node_type, "device_type": device_type})

    # ── 选择驱动 ─────────────────────────────────────────────────────────────
    if device_type == "harmony":
        from open_test_agent.drivers.hdc import run_action_at, capture_screenshot, run_app_ui_action
    else:
        from open_test_agent.drivers.adb import run_action_at, capture_screenshot, run_app_ui_action

    success: bool = False
    message: str  = ""
    shot_bytes: bytes | None = None

    try:
        if node_type not in _APP_UI_NODE_TYPES:
            success, message = False, f"Agent 不支持的节点类型: {node_type}"

        elif action in _NO_LOCATE_ACTIONS:
            # ── 无需定位，直接执行 ──────────────────────────────────────────
            success, message = await run_action_at(action, None, body.node_data, serial)

        elif action == "wait_element":
            # ── wait_element：轮询 locate_element，直至成功或超时 ───────────
            from open_test_agent.locator import locate_element
            strategies = _get_strategies(body.node_data)
            screenshot_fn = lambda: capture_screenshot(serial)  # noqa: E731
            loop = asyncio.get_event_loop()
            deadline = loop.time() + _WAIT_ELEMENT_TIMEOUT
            while True:
                try:
                    result = await locate_element(strategies, device_type, screenshot_fn, serial)
                    success = True
                    message = f"element appeared at ({result.x},{result.y}) via [{result.strategy}]"
                    break
                except RuntimeError:
                    if loop.time() >= deadline:
                        success = False
                        message = f"wait_element 超时（{_WAIT_ELEMENT_TIMEOUT}s），所有策略均失败"
                        break
                    await asyncio.sleep(_WAIT_ELEMENT_INTERVAL)

        else:
            # ── 需要定位的 action：混合策略定位 → 执行动作 ───────────────────
            strategies = _get_strategies(body.node_data)

            if strategies:
                # 有配置 locate_strategies（或旧 selector），走混合定位
                from open_test_agent.locator import locate_element
                screenshot_fn = lambda: capture_screenshot(serial)  # noqa: E731
                locate_result = await locate_element(
                    strategies, device_type, screenshot_fn, serial
                )
                _log("info", body.node_id,
                     f"  ✔ 定位成功 ({locate_result.x},{locate_result.y}) "
                     f"via [{locate_result.strategy}]"
                     + (f"，降级策略: {locate_result.errors}" if locate_result.errors else ""),
                     {"strategy": locate_result.strategy})

                # OCR 找到的文字注入到 node_data，供 get_text 动作读取
                node_data_ext = dict(body.node_data)
                if locate_result.found_text is not None:
                    node_data_ext["_ocr_text"] = locate_result.found_text

                success, message = await run_action_at(
                    action, (locate_result.x, locate_result.y), node_data_ext, serial
                )
            else:
                # 无任何定位配置，回退到旧 run_app_ui_action（含自行 _find_element）
                success, message, shot_bytes = await run_app_ui_action(body.node_data)

    except Exception as exc:
        success, message = False, str(exc)

    # ── 执行后截图（失败不影响主流程） ────────────────────────────────────────
    if shot_bytes is None:
        try:
            shot_bytes, _, _ = await asyncio.wait_for(capture_screenshot(serial), timeout=10.0)
        except Exception:
            shot_bytes = None

    screenshot_b64: str | None = None
    if shot_bytes:
        import base64 as _b64
        screenshot_b64 = "data:image/png;base64," + _b64.b64encode(shot_bytes).decode()

    duration = time.perf_counter() - t0
    icon = "✓" if success else "✗"
    _log(
        "success" if success else "error",
        body.node_id,
        f"{icon} {message}  ({duration * 1000:.0f}ms)",
        {"success": success, "duration_ms": round(duration * 1000)},
    )
    return ExecuteResponse(
        success=success, message=message, duration=duration, screenshot=screenshot_b64
    )


def _get_strategies(node_data: dict) -> list[dict]:
    """
    从 node_data 中获取 locate_strategies 列表。
    向后兼容：若无 locate_strategies 但有旧 selector，自动转换首条 selector 策略。
    """
    strategies: list[dict] = node_data.get("locate_strategies") or []
    if not strategies:
        selector = node_data.get("selector", "").strip()
        if selector:
            strategies = [{"type": "selector", "value": selector, "enabled": True}]
    return strategies



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
