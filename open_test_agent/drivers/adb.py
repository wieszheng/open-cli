"""
ADB 驱动（基于 adbutils）
封装 appUiAction 节点的实际执行逻辑

STUB 模式：所有操作均模拟执行（固定成功 + 随机延迟），无需真机。
真实模式：依赖 adbutils，通过 ADB 协议直接与设备通信，无需在设备上安装额外服务。

接口分层：
  run_app_ui_action(data)          — 旧接口，自含定位+动作，向后兼容
  run_action_at(action, coords, data, serial) — 新接口，坐标已由 locator 层计算
"""
import asyncio
import subprocess

STUB = False  # True = 模拟执行；False = 真实 ADB 执行


# ── 设备列表 ──────────────────────────────────────────────────────────────────

def check_adb() -> list[str]:
    """返回已连接设备序列号列表。STUB 模式返回虚拟设备。"""
    if STUB:
        return ["stub-device-001"]
    try:
        import adbutils
        return [d.serial for d in adbutils.adb.device_list()]
    except Exception:
        return []


# ── appUiAction 入口 ──────────────────────────────────────────────────────────

async def run_app_ui_action(data: dict) -> tuple[bool, str, bytes | None]:
    """执行 appUiAction 节点。STUB 模式下模拟执行。返回 (success, message, screenshot_png)。"""
    if STUB:
        ok, msg = await _stub_action(data)
        return ok, msg, None
    try:
        import adbutils  # noqa: F401
    except ImportError:
        return False, "adbutils 未安装，请执行: pip install adbutils", None
    action = data.get("action", "click")
    serial = data.get("device_serial") or None
    loop = asyncio.get_event_loop()
    try:
        ok, msg = await asyncio.wait_for(
            loop.run_in_executor(None, _execute, action, data, serial),
            timeout=60.0,
        )
    except asyncio.TimeoutError:
        return False, f"操作超时（60s）: {action}", None
    # 执行后截图（截图失败不影响主流程）
    shot: bytes | None = None
    try:
        shot, _, _ = await asyncio.wait_for(capture_screenshot(serial), timeout=10.0)
    except Exception:
        pass
    return ok, msg, shot


# ── 新接口：动作执行（坐标由外部 locator 传入） ───────────────────────────────

async def run_action_at(
    action: str,
    coords: tuple[int, int] | None,
    data: dict,
    serial: str | None,
) -> tuple[bool, str]:
    """
    执行动作，坐标由混合定位层（locator.py）预先计算并传入。
    coords=None 表示该动作不需要元素坐标（如 launch_app / swipe / press_key）。
    返回 (success, message)，不含截图（由 agent_server 统一处理）。
    """
    if STUB:
        return await _stub_action(data)
    try:
        import adbutils  # noqa: F401
    except ImportError:
        return False, "adbutils 未安装，请执行: pip install adbutils"

    loop = asyncio.get_event_loop()
    try:
        ok, msg = await asyncio.wait_for(
            loop.run_in_executor(None, _execute_at, action, coords, data, serial),
            timeout=60.0,
        )
    except asyncio.TimeoutError:
        return False, f"操作超时（60s）: {action}"
    return ok, msg


def _execute_at(
    action: str,
    coords: tuple[int, int] | None,
    data: dict,
    serial: str | None,
) -> tuple[bool, str]:
    """
    同步动作执行器。coords 为已经计算好的中心坐标，None 表示无需定位。
    与旧 _execute() 共存，不含自行定位逻辑。
    """
    import time

    try:
        d = _get_device(serial)
        x, y = coords if coords is not None else (0, 0)

        # ── launch_app ────────────────────────────────────────────────────
        if action == "launch_app":
            app_id = data.get("app_id", "")
            launch_type = data.get("launch_type", "cold")
            if not app_id:
                return False, "app_id 不能为空"
            if launch_type == "cold":
                try:
                    d.app_stop(app_id)
                except Exception:
                    pass
            d.app_start(app_id)
            return True, f"已{'冷' if launch_type == 'cold' else '热'}启动 {app_id}"

        # ── tap_xy ────────────────────────────────────────────────────────
        if action == "tap_xy":
            coords_str = data.get("coordinates", "")
            if coords_str and "," in coords_str:
                try:
                    tx, ty = [int(v.strip()) for v in coords_str.split(",", 1)]
                    d.click(tx, ty)
                    return True, f"tapped ({tx}, {ty})"
                except ValueError:
                    return False, f"坐标解析失败: {coords_str!r}"
            return False, "coordinates 格式错误，应为 x,y"

        # ── swipe ─────────────────────────────────────────────────────────
        if action == "swipe":
            direction = data.get("value", "up")
            w, h = d.window_size()
            cx_s, cy_s = w // 2, h // 2
            coords_map = {
                "up":    (cx_s, int(h * 0.7), cx_s, int(h * 0.3)),
                "down":  (cx_s, int(h * 0.3), cx_s, int(h * 0.7)),
                "left":  (int(w * 0.7), cy_s, int(w * 0.3), cy_s),
                "right": (int(w * 0.3), cy_s, int(w * 0.7), cy_s),
            }
            x1, y1, x2, y2 = coords_map.get(direction, coords_map["up"])
            d.swipe(x1, y1, x2, y2, duration=0.4)
            return True, f"swiped {direction}"

        # ── screenshot ────────────────────────────────────────────────────
        if action == "screenshot":
            import io
            path = f"/tmp/screenshot_{int(time.time())}.png"
            img = d.screenshot()
            img.save(path)
            return True, f"screenshot saved: {path}"

        # ── press_key ─────────────────────────────────────────────────────
        if action == "press_key":
            key_map = {
                "home": "HOME", "back": "BACK", "recent": "APP_SWITCH",
                "volume_up": "VOLUME_UP", "volume_down": "VOLUME_DOWN",
                "power": "POWER", "enter": "ENTER",
            }
            key = data.get("key_code", "home")
            d.keyevent(key_map.get(key, key.upper()))
            return True, f"pressed key: {key}"

        # ── coords 必须有效才能继续 ───────────────────────────────────────
        if coords is None:
            return False, f"动作 {action!r} 需要元素坐标，但 locator 未提供"

        # ── click ─────────────────────────────────────────────────────────
        if action == "click":
            d.click(x, y)
            return True, f"clicked ({x},{y})"

        # ── double_click ──────────────────────────────────────────────────
        if action == "double_click":
            d.click(x, y)
            time.sleep(0.08)
            d.click(x, y)
            return True, f"double_clicked ({x},{y})"

        # ── long_press ────────────────────────────────────────────────────
        if action == "long_press":
            ms = int(data.get("duration_ms", 1000))
            d.swipe(x, y, x, y, duration=ms / 1000.0)
            return True, f"long_pressed ({x},{y}) for {ms}ms"

        # ── type ──────────────────────────────────────────────────────────
        if action == "type":
            value = data.get("value", "")
            d.click(x, y)
            time.sleep(0.2)
            d.send_keys(value)
            return True, f"typed {value!r} into ({x},{y})"

        # ── clear_text ────────────────────────────────────────────────────
        if action == "clear_text":
            d.click(x, y)
            time.sleep(0.2)
            d.keyevent(277)   # KEYCODE_CTRL_A
            time.sleep(0.1)
            d.keyevent(67)    # KEYCODE_DEL
            return True, f"cleared text at ({x},{y})"

        # ── wait_element ──────────────────────────────────────────────────
        # 此动作依赖 locator 层重试，到达这里说明已定位成功
        if action == "wait_element":
            return True, f"element appeared at ({x},{y})"

        # ── get_text ──────────────────────────────────────────────────────
        if action == "get_text":
            import xml.etree.ElementTree as ET
            import re
            var_name = data.get("var_name", "result")
            # 若 locator 为 OCR 策略，found_text 由 agent_server 层注入在 data["_ocr_text"]
            ocr_text = data.get("_ocr_text")
            if ocr_text is not None:
                return True, f"{var_name} = {ocr_text!r}"
            # 否则从 XML 层级树按坐标反查
            xml_str = d.dump_hierarchy()
            root = ET.fromstring(xml_str)
            pat = re.compile(r'\[(\d+),(\d+)\]\[(\d+),(\d+)\]')
            for elem in root.iter():
                m = pat.match(elem.get("bounds", ""))
                if m:
                    x1e, y1e, x2e, y2e = map(int, m.groups())
                    if (x1e + x2e) // 2 == x and (y1e + y2e) // 2 == y:
                        text = elem.get("text", "")
                        return True, f"{var_name} = {text!r}"
            return True, f"{var_name} = ''"

        return False, f"不支持的 action: {action}"

    except Exception as exc:
        return False, str(exc)


# ── 截图 API ──────────────────────────────────────────────────────────────────

async def capture_screenshot(serial: str | None = None) -> tuple[bytes, int, int]:
    """返回 (png_bytes, device_width, device_height)。"""
    if STUB:
        await asyncio.sleep(0.4)
        return _make_stub_png(), 1080, 1920
    return await asyncio.get_event_loop().run_in_executor(
        None, _capture_screenshot_sync, serial
    )


def _capture_screenshot_sync(serial: str | None) -> tuple[bytes, int, int]:
    import io
    d = _get_device(serial)
    w, h = d.window_size()
    img = d.screenshot()          # PIL Image
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue(), w, h


def _dump_hierarchy(serial: str | None) -> dict:
    """
    获取 Android UI 层级树，解析 XML 并转为嵌套 dict 供 agent_server /layout 使用。
    返回根节点 dict，子节点在 "node" 列表中。
    """
    import xml.etree.ElementTree as ET

    d = _get_device(serial)
    xml_str = d.dump_hierarchy()
    root = ET.fromstring(xml_str)

    def _to_dict(elem) -> dict:
        node: dict = {k: v for k, v in elem.attrib.items() if k.startswith("@") or True}
        # attrib keys 本身已是 @resource-id / @text / @bounds 等
        node.update({f"@{k}": v for k, v in elem.attrib.items()})
        children = [_to_dict(child) for child in elem]
        if children:
            node["node"] = children
        return node

    return _to_dict(root)


# ── 真实执行 ──────────────────────────────────────────────────────────────────

def _get_device(serial: str | None):
    """获取 AdbDevice 实例；无 serial 时取第一台设备。"""
    import adbutils
    devices = adbutils.adb.device_list()
    if not devices:
        raise RuntimeError("没有已连接的设备，请检查 adb devices")
    if serial:
        for d in devices:
            if d.serial == serial:
                return d
        raise RuntimeError(f"设备 {serial!r} 未连接")
    return devices[0]


def _find_element(d, selector: str) -> tuple[int, int]:
    """
    在 UI 层级树中查找元素，返回中心坐标 (x, y)。

    支持格式：
    - resource-id:  com.example.app:id/btn_login
    - 简化 XPath:   //android.widget.Button[@text='OK']
                    //android.widget.EditText[@resource-id='com.app:id/input']
    - 纯文本:       OK  （匹配 text 或 content-desc 属性）
    """
    import xml.etree.ElementTree as ET
    import re

    xml_str = d.dump_hierarchy()
    root    = ET.fromstring(xml_str)
    node    = None

    if selector.startswith("//"):
        # 解析简化 XPath: //ClassName[@attr='value']
        m = re.match(r'^//([^\[/@]+)(?:\[@?([^=\]]+)=["\']([^"\']+)["\']\])?$', selector)
        if m:
            cls, attr, val = m.groups()
            short_cls = cls.split(".")[-1]   # 取类名末段宽松匹配
            if attr and val:
                for elem in root.iter():
                    if short_cls in elem.get("class", "") and elem.get(attr) == val:
                        node = elem; break
                if node is None:             # 降级：只按属性查
                    node = root.find(f".//*[@{attr}='{val}']")
            else:
                for elem in root.iter():
                    if short_cls in elem.get("class", ""):
                        node = elem; break
        if node is None:
            raise ValueError(f"XPath 未找到元素: {selector!r}")

    elif ":" in selector and "/" in selector:
        # resource-id 格式
        node = root.find(f".//*[@resource-id='{selector}']")
        if node is None:
            raise ValueError(f"resource-id 未找到元素: {selector!r}")

    else:
        # 纯文本 / content-desc
        node = root.find(f".//*[@text='{selector}']")
        if node is None:
            node = root.find(f".//*[@content-desc='{selector}']")
        if node is None:
            raise ValueError(f"text/desc 未找到元素: {selector!r}")

    bounds = node.get("bounds", "")
    m2 = re.match(r'\[(\d+),(\d+)\]\[(\d+),(\d+)\]', bounds)
    if not m2:
        raise ValueError(f"无效的 bounds: {bounds!r}")
    x1, y1, x2, y2 = map(int, m2.groups())
    return (x1 + x2) // 2, (y1 + y2) // 2


def _execute(action: str, data: dict, serial: str | None) -> tuple[bool, str]:
    import time

    try:
        d = _get_device(serial)

        # ── launch_app ────────────────────────────────────────────────────
        if action == "launch_app":
            app_id = data.get("app_id", "")
            if not app_id:
                return False, "app_id 不能为空"
            launch_type = data.get("launch_type", "cold")
            if launch_type == "cold":
                try:
                    d.app_stop(app_id)
                except Exception:
                    pass
            d.app_start(app_id)
            return True, f"已{'冷' if launch_type == 'cold' else '热'}启动 {app_id}"

        # ── tap_xy ────────────────────────────────────────────────────────
        if action == "tap_xy":
            coords = data.get("coordinates", "")
            if not coords or "," not in coords:
                return False, "coordinates 格式错误，应为 x,y"
            try:
                x, y = [int(v.strip()) for v in coords.split(",", 1)]
            except ValueError:
                return False, f"坐标解析失败: {coords!r}"
            d.click(x, y)
            return True, f"tapped ({x}, {y})"

        # ── swipe ─────────────────────────────────────────────────────────
        if action == "swipe":
            direction = data.get("value", "up")
            w, h = d.window_size()
            cx, cy = w // 2, h // 2
            coords_map = {
                "up":    (cx, int(h * 0.7), cx, int(h * 0.3)),
                "down":  (cx, int(h * 0.3), cx, int(h * 0.7)),
                "left":  (int(w * 0.7), cy, int(w * 0.3), cy),
                "right": (int(w * 0.3), cy, int(w * 0.7), cy),
            }
            x1, y1, x2, y2 = coords_map.get(direction, coords_map["up"])
            d.swipe(x1, y1, x2, y2, duration=0.4)
            return True, f"swiped {direction}"

        # ── screenshot ────────────────────────────────────────────────────
        if action == "screenshot":
            import io
            path = f"/tmp/screenshot_{int(time.time())}.png"
            img = d.screenshot()
            img.save(path)
            return True, f"screenshot saved: {path}"

        # ── press_key ─────────────────────────────────────────────────────
        if action == "press_key":
            key_map = {
                "home":        "HOME",
                "back":        "BACK",
                "recent":      "APP_SWITCH",
                "volume_up":   "VOLUME_UP",
                "volume_down": "VOLUME_DOWN",
                "power":       "POWER",
                "enter":       "ENTER",
            }
            key = data.get("key_code", "home")
            d.keyevent(key_map.get(key, key.upper()))
            return True, f"pressed key: {key}"

        # ── selector-based actions ────────────────────────────────────────
        selector = data.get("selector", "")
        if not selector:
            return False, "selector 不能为空"

        if action == "click":
            x, y = _find_element(d, selector)
            d.click(x, y)
            return True, f"clicked {selector} at ({x},{y})"

        if action == "double_click":
            x, y = _find_element(d, selector)
            d.click(x, y)
            time.sleep(0.08)
            d.click(x, y)
            return True, f"double_clicked {selector} at ({x},{y})"

        if action == "long_press":
            x, y = _find_element(d, selector)
            ms = int(data.get("duration_ms", 1000))
            d.swipe(x, y, x, y, duration=ms / 1000.0)
            return True, f"long_pressed {selector} for {ms}ms"

        if action == "type":
            value = data.get("value", "")
            x, y = _find_element(d, selector)
            d.click(x, y)
            time.sleep(0.2)
            d.send_keys(value)
            return True, f"typed {value!r} into {selector}"

        if action == "clear_text":
            x, y = _find_element(d, selector)
            d.click(x, y)
            time.sleep(0.2)
            d.keyevent(277)   # KEYCODE_CTRL_A  全选
            time.sleep(0.1)
            d.keyevent(67)    # KEYCODE_DEL     删除
            return True, f"cleared text in {selector}"

        if action == "wait_element":
            timeout  = 10
            interval = 0.5
            deadline = time.time() + timeout
            while time.time() < deadline:
                try:
                    _find_element(d, selector)
                    return True, f"element appeared: {selector}"
                except ValueError:
                    time.sleep(interval)
            return False, f"element not found within {timeout}s: {selector}"

        if action == "get_text":
            import xml.etree.ElementTree as ET
            import re
            var_name = data.get("var_name", "result")
            # 先定位元素中心，再在 XML 中反查 text 属性
            cx, cy = _find_element(d, selector)
            xml_str = d.dump_hierarchy()
            root = ET.fromstring(xml_str)
            pat  = re.compile(r'\[(\d+),(\d+)\]\[(\d+),(\d+)\]')
            for elem in root.iter():
                m = pat.match(elem.get("bounds", ""))
                if m:
                    x1, y1, x2, y2 = map(int, m.groups())
                    if (x1 + x2) // 2 == cx and (y1 + y2) // 2 == cy:
                        text = elem.get("text", "")
                        return True, f"{var_name} = {text!r}"
            return True, f"{var_name} = ''"

        return False, f"不支持的 action: {action}"

    except Exception as exc:
        return False, str(exc)


# ── STUB 模拟 ─────────────────────────────────────────────────────────────────

async def _stub_action(data: dict) -> tuple[bool, str]:
    """模拟 ADB 执行，随机延迟 0.3~0.8s 后返回成功。"""
    import random
    action = data.get("action", "click")
    await asyncio.sleep(random.uniform(0.3, 0.8))

    if action == "launch_app":
        return True, f"[stub] launched {data.get('app_id', 'com.example.app')}"

    sel = data.get("selector", "<selector>")
    val = data.get("value", "")

    match action:
        case "click":        return True, f"[stub] clicked: {sel}"
        case "long_press":   return True, f"[stub] long_pressed {sel} for {data.get('duration_ms',1000)}ms"
        case "double_click": return True, f"[stub] double_clicked: {sel}"
        case "type":         return True, f"[stub] typed {val!r} into {sel}"
        case "clear_text":   return True, f"[stub] cleared text in {sel}"
        case "swipe":        return True, f"[stub] swiped {val or 'up'}"
        case "tap_xy":       return True, f"[stub] tapped xy({data.get('coordinates','0,0')})"
        case "wait_element": return True, f"[stub] element appeared: {sel}"
        case "get_text":     return True, f"[stub] {data.get('var_name','result')} = 'stub_text'"
        case "screenshot":   return True, "[stub] screenshot saved: /tmp/stub_screenshot.png"
        case "press_key":    return True, f"[stub] pressed key: {data.get('key_code','home')}"
        case _:              return True, f"[stub] {action} OK"


# ── Stub PNG 生成 ─────────────────────────────────────────────────────────────

def _make_stub_png() -> bytes:
    """纯 stdlib 生成一张 360×640 仿手机界面 PNG，无需第三方依赖。"""
    import zlib, struct

    W, H = 360, 640

    def chunk(tag: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)

    def row(y: int) -> bytes:
        if y < 56:
            return b"\x00" + bytes([22, 22, 38]) * W
        if y >= H - 48:
            return b"\x00" + bytes([28, 28, 28]) * W
        v = 38 + (y - 56) * 18 // (H - 104)
        base = bytearray([v, v, v] * W)
        if y < 112:
            for x in range(W): base[x*3:x*3+3] = [32, 36, 64]
        elif 160 <= y <= 196:
            for x in range(24, 336): base[x*3:x*3+3] = [52, 52, 66]
        elif 220 <= y <= 256:
            for x in range(24, 336): base[x*3:x*3+3] = [52, 52, 66]
        elif 300 <= y <= 348:
            for x in range(W):
                base[x*3:x*3+3] = [56, 110, 220] if 48 <= x <= 312 else [v, v, v]
        elif 372 <= y <= 408:
            for x in range(W):
                if x in (48, 312) or y in (372, 408): base[x*3:x*3+3] = [80, 80, 120]
                elif 48 < x < 312:                    base[x*3:x*3+3] = [44, 44, 58]
        elif y > 430 and (y - 430) % 60 == 0:
            for x in range(W): base[x*3:x*3+3] = [55, 55, 55]
        return b"\x00" + bytes(base)

    raw  = b"".join(row(y) for y in range(H))
    ihdr = chunk(b"IHDR", struct.pack(">IIBBBBB", W, H, 8, 2, 0, 0, 0))
    idat = chunk(b"IDAT", zlib.compress(raw, 1))
    return b"\x89PNG\r\n\x1a\n" + ihdr + idat + chunk(b"IEND", b"")
