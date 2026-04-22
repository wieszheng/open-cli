"""
混合模式多策略元素定位器
策略优先级（依次尝试，首个成功立即返回）：
  1. selector    — 结构树定位（ADB UIAutomator / HDC uitest）
  2. image       — 图像识别（OpenCV 模板匹配）
  3. ocr         — OCR 文字识别（PaddleOCR）
  4. ai_vision   — AI 视觉（OpenAI / Ollama 多模态 API）

node.data 中 locate_strategies 格式示例：
  [
    {"type": "selector", "value": "com.app:id/btn", "platform": "android", "enabled": true},
    {"type": "selector", "value": "#loginBtn",      "platform": "harmony",  "enabled": true},
    {"type": "image",    "ref_image": "data:image/png;base64,...", "threshold": 0.85},
    {"type": "ocr",      "text": "登录", "match": "contains"},
    {"type": "ai_vision","prompt": "找到页面上的登录按钮", "model": "gpt-4o"},
  ]
"""

from __future__ import annotations

import asyncio
import base64
import os
import re
from dataclasses import dataclass, field
from typing import Callable, Awaitable


# ---------------------------------------------------------------------------
# 定位结果（携带额外信息供后续动作使用）
# ---------------------------------------------------------------------------

@dataclass
class LocateResult:
    x: int
    y: int
    strategy: str                   # 哪种策略成功
    found_text: str | None = None   # OCR 策略时填充，可供 get_text 动作直接使用
    errors: list[str] = field(default_factory=list)  # 已尝试过的失败记录


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

async def locate_element(
    strategies: list[dict],
    device_type: str,
    screenshot_fn: Callable[[], Awaitable[tuple[bytes, int, int]]],
    serial: str | None,
) -> LocateResult:
    """
    按 strategies 列表顺序依次尝试定位，返回 LocateResult。
    所有策略失败则抛出 RuntimeError（携带各策略失败原因）。

    Parameters
    ----------
    strategies    : locate_strategies 列表
    device_type   : "android" | "harmony"
    screenshot_fn : async () -> (png_bytes, width, height)
    serial        : 设备序列号，None 表示取第一台
    """
    errors: list[str] = []

    for s in strategies:
        if not s.get("enabled", True):
            continue

        platform = s.get("platform")
        if platform and platform != device_type:
            continue  # 平台不匹配，跳过此条策略

        stype = s.get("type", "")
        hint = s.get("value") or s.get("text") or s.get("prompt") or ""

        try:
            if stype == "selector":
                x, y = await _locate_selector(s, device_type, serial)
                return LocateResult(x, y, "selector", errors=errors)

            elif stype == "image":
                x, y = await _locate_image(s, screenshot_fn)
                return LocateResult(x, y, "image", errors=errors)

            elif stype == "ocr":
                x, y, text = await _locate_ocr(s, screenshot_fn)
                return LocateResult(x, y, "ocr", found_text=text, errors=errors)

            elif stype == "ai_vision":
                x, y = await _locate_ai(s, screenshot_fn)
                return LocateResult(x, y, "ai_vision", errors=errors)

            else:
                errors.append(f"[{stype}] 未知策略类型")

        except Exception as exc:
            errors.append(f"[{stype}:{hint[:40]}] {exc}")

    raise RuntimeError(
        "所有定位策略均失败:\n" + "\n".join(f"  · {e}" for e in errors)
    )


# ---------------------------------------------------------------------------
# 策略 1：结构树定位
# ---------------------------------------------------------------------------

async def _locate_selector(
    s: dict,
    device_type: str,
    serial: str | None,
) -> tuple[int, int]:
    """调用驱动层的 _find_element，在 UI 层级树中查找元素坐标。"""
    value = s.get("value", "").strip()
    if not value:
        raise ValueError("selector value 未配置")

    loop = asyncio.get_event_loop()

    if device_type == "harmony":
        from open_test_agent.drivers.hdc import _find_element
        return await loop.run_in_executor(None, _find_element, serial, value)
    else:
        from open_test_agent.drivers.adb import _find_element, _get_device
        d = await loop.run_in_executor(None, _get_device, serial)
        return await loop.run_in_executor(None, _find_element, d, value)


# ---------------------------------------------------------------------------
# 策略 2：图像识别定位
# ---------------------------------------------------------------------------

async def _locate_image(
    s: dict,
    screenshot_fn: Callable[[], Awaitable[tuple[bytes, int, int]]],
) -> tuple[int, int]:
    """
    OpenCV 模板匹配。
    s["ref_image"]  : data:image/png;base64,xxx  （从 node.data 读取）
    s["threshold"]  : 匹配置信度阈值，默认 0.85
    """
    try:
        import cv2
        import numpy as np
    except ImportError:
        raise RuntimeError(
            "图像识别策略需要安装 opencv：pip install opencv-python-headless"
        )

    ref_b64 = s.get("ref_image", "")
    if not ref_b64:
        raise ValueError("ref_image 未配置")
    threshold = float(s.get("threshold", 0.85))

    # 解码参考模板
    raw = base64.b64decode(ref_b64.split(",")[-1])
    arr = np.frombuffer(raw, np.uint8)
    template = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if template is None:
        raise ValueError("ref_image 解码失败，请重新截取")

    # 获取当前屏幕截图
    shot_bytes, _, _ = await screenshot_fn()
    sarr = np.frombuffer(shot_bytes, np.uint8)
    screen = cv2.imdecode(sarr, cv2.IMREAD_COLOR)

    # 模板匹配
    result = cv2.matchTemplate(screen, template, cv2.TM_CCOEFF_NORMED)
    _, max_val, _, max_loc = cv2.minMaxLoc(result)

    if max_val < threshold:
        raise ValueError(
            f"图像匹配置信度 {max_val:.3f} 低于阈值 {threshold}，请更新参考截图"
        )

    th, tw = template.shape[:2]
    cx = max_loc[0] + tw // 2
    cy = max_loc[1] + th // 2
    return cx, cy


# ---------------------------------------------------------------------------
# 策略 3：OCR 文字识别定位
# ---------------------------------------------------------------------------

async def _locate_ocr(
    s: dict,
    screenshot_fn: Callable[[], Awaitable[tuple[bytes, int, int]]],
) -> tuple[int, int, str]:
    """
    使用 PaddleOCR 在截图中识别文字位置。
    s["text"]  : 目标文字
    s["match"] : "exact" | "contains"（默认 contains）
    返回 (cx, cy, matched_text)
    """
    try:
        from paddleocr import PaddleOCR  # type: ignore[import]
        import numpy as np
        import cv2  # type: ignore[import]
    except ImportError:
        raise RuntimeError(
            "OCR 定位策略需要安装 PaddleOCR：pip install paddleocr paddlepaddle"
        )

    target = s.get("text", "").strip()
    match_mode = s.get("match", "contains")
    if not target:
        raise ValueError("OCR text 未配置")

    shot_bytes, _, _ = await screenshot_fn()
    arr = np.frombuffer(shot_bytes, np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)

    loop = asyncio.get_event_loop()
    ocr = PaddleOCR(use_angle_cls=True, lang="ch", show_log=False)

    # 在线程池中运行 OCR（CPU 密集）
    raw_result = await loop.run_in_executor(None, ocr.ocr, img, True)

    for line in (raw_result[0] or []):
        box, (text, _conf) = line
        matched = (
            (match_mode == "exact" and text == target)
            or (match_mode == "contains" and target in text)
        )
        if matched:
            xs = [p[0] for p in box]
            ys = [p[1] for p in box]
            cx = int(sum(xs) / 4)
            cy = int(sum(ys) / 4)
            return cx, cy, text

    raise ValueError(f"OCR 未找到文字 {target!r}（match={match_mode}）")


# ---------------------------------------------------------------------------
# 策略 4：AI 视觉定位
# ---------------------------------------------------------------------------

async def _locate_ai(
    s: dict,
    screenshot_fn: Callable[[], Awaitable[tuple[bytes, int, int]]],
) -> tuple[int, int]:
    """
    发送截图给多模态 LLM，用自然语言描述找元素坐标。

    s["prompt"]     : 自然语言描述，如 "找到页面上标有「登录」的按钮"
    s["model"]      : 模型名，默认 "gpt-4o"
    s["api_base"]   : 可选，覆盖环境变量 OPENAI_API_BASE
    s["api_key"]    : 可选，覆盖环境变量 OPENAI_API_KEY

    LLM 返回约定（JSON）：{"x": <center_x>, "y": <center_y>}
    """
    try:
        import httpx
    except ImportError:
        raise RuntimeError("AI 视觉策略需要安装 httpx：pip install httpx")

    prompt = s.get("prompt", "").strip()
    if not prompt:
        raise ValueError("ai_vision prompt 未配置")

    model    = s.get("model", "gpt-4o")
    api_base = s.get("api_base") or os.getenv("OPENAI_API_BASE", "https://api.openai.com/v1")
    api_key  = s.get("api_key")  or os.getenv("OPENAI_API_KEY", "")

    shot_bytes, w, h = await screenshot_fn()
    img_b64 = base64.b64encode(shot_bytes).decode()

    system_msg = (
        f"You are a mobile UI element locator. "
        f"The screenshot is {w}×{h} pixels. "
        f"Find the UI element described by the user and return ONLY a raw JSON object "
        f'with integer pixel coordinates of its center: {{"x": <int>, "y": <int>}}. '
        f"No markdown, no explanation."
    )

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_msg},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{img_b64}"},
                    },
                ],
            },
        ],
        "max_tokens": 64,
        "temperature": 0,
    }

    async with httpx.AsyncClient(timeout=45) as client:
        resp = await client.post(
            f"{api_base}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json=payload,
        )
        resp.raise_for_status()

    content = resp.json()["choices"][0]["message"]["content"].strip()

    # 从返回文本中健壮地提取 JSON
    m = re.search(r'\{[^{}]*"x"\s*:\s*\d+[^{}]*"y"\s*:\s*\d+[^{}]*\}', content, re.DOTALL)
    if not m:
        m = re.search(r'\{[^{}]*"y"\s*:\s*\d+[^{}]*"x"\s*:\s*\d+[^{}]*\}', content, re.DOTALL)
    if not m:
        raise ValueError(f"AI 返回格式无法解析: {content[:200]!r}")

    import json as _json
    coords = _json.loads(m.group())
    x = int(coords["x"])
    y = int(coords["y"])

    # 基本合法性校验
    if not (0 <= x <= w * 2 and 0 <= y <= h * 2):
        raise ValueError(f"AI 返回坐标越界: ({x},{y})，屏幕 {w}×{h}")

    return x, y
