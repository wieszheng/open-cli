"""
HDC 驱动（鸿蒙设备，基于 hdc 命令行）
封装 appUiAction 节点的实际执行逻辑

依赖：hdc 命令行工具（随 DevEco Studio / OpenHarmony SDK 安装）
  - 触控注入：uitest uiInput click/swipe/fling/longClick/doubleClick/drag/keyEvent/inputText
  - 截图：     snapshot_display -f（推荐，性能远优于 uitest screenCap）
  - 布局树：   uitest dumpLayout + hdc file recv
  - 应用启动： aa start -a <abilityName> -b <bundleName>
  - 应用退出： aa force-stop <bundleName>

接口分层：
  run_app_ui_action(data)                     — 旧接口，自含定位+动作，向后兼容
  run_action_at(action, coords, data, serial) — 新接口，坐标已由 locator 层计算
"""
import asyncio
import json
import os
import re
import shutil
import struct
import subprocess
import tempfile
import time


# ── hdc 路径解析 ──────────────────────────────────────────────────────────────

def _hdc_bin() -> str:
    """
    返回 hdc 可执行文件路径。
    按顺序查找：shutil.which → 常见 SDK 安装路径。
    """
    found = shutil.which("hdc")
    if found:
        return found
    candidates = [
        # macOS DevEco Studio / OpenHarmony SDK 默认路径
        os.path.expanduser("~/Library/command-line-tools/sdk/default/openharmony/toolchains/hdc"),
        os.path.expanduser("~/Library/Huawei/Sdk/openharmony/toolchains/hdc"),
        "/usr/local/bin/hdc",
    ]
    for c in candidates:
        if os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return "hdc"   # 最后兜底，让系统自己找


_HDC = _hdc_bin()


# ── hdc 命令执行工具 ──────────────────────────────────────────────────────────

def _run(args: list[str], serial: str | None = None, timeout: int = 15) -> str:
    """执行 hdc 命令，返回 stdout 字符串，失败时抛出 RuntimeError。"""
    cmd = [_HDC]
    if serial:
        cmd += ["-t", serial]
    cmd += args
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    output = (result.stdout + result.stderr).strip()
    if result.returncode != 0 and output:
        raise RuntimeError(output)
    return output


def _shell(cmd: str, serial: str | None = None, timeout: int = 15) -> str:
    """在设备上执行 shell 命令。"""
    return _run(["shell", cmd], serial=serial, timeout=timeout)


# ── 设备列表 ──────────────────────────────────────────────────────────────────

def check_hdc() -> list[str]:
    """返回已连接鸿蒙设备序列号列表。"""
    try:
        out = subprocess.run(
            [_HDC, "list", "targets"],
            capture_output=True, text=True, timeout=5
        ).stdout.strip()
        return [line.strip() for line in out.splitlines() if line.strip() and "[Empty]" not in line]
    except Exception:
        return []


# ── appUiAction 入口 ──────────────────────────────────────────────────────────

async def run_app_ui_action(data: dict) -> tuple[bool, str, bytes | None]:
    """执行 appUiAction 节点（HarmonyOS）。返回 (success, message, screenshot_png)。"""
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
    返回 (success, message)，不含截图。
    """
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
    HDC 同步动作执行器。coords 为已经计算好的中心坐标，None 表示无需定位。
    """
    try:
        x, y = coords if coords is not None else (0, 0)

        # ── launch_app ────────────────────────────────────────────────────
        if action == "launch_app":
            app_id = data.get("app_id", "")
            if not app_id:
                return False, "app_id 不能为空"
            bundle, ability = (app_id.split("/", 1) if "/" in app_id
                               else (app_id, "EntryAbility"))
            launch_type = data.get("launch_type", "cold")
            if launch_type == "cold":
                # 冷起：先强制关闭再启动
                try:
                    _shell(f"aa force-stop {bundle}", serial=serial)
                except Exception:
                    pass
            # hdc.md: aa start -a {abilityName} -b {bundleName}
            _shell(f"aa start -a {ability} -b {bundle}", serial=serial)
            return True, f"已{'冷' if launch_type == 'cold' else '热'}启动 {app_id}"

        # ── stop_app ──────────────────────────────────────────────────────
        if action == "stop_app":
            app_id = data.get("app_id", "")
            if not app_id:
                return False, "app_id 不能为空"
            bundle = app_id.split("/", 1)[0]
            _shell(f"aa force-stop {bundle}", serial=serial)
            return True, f"已退出 {bundle}"

        # ── tap_xy ────────────────────────────────────────────────────────
        if action == "tap_xy":
            coords_str = data.get("coordinates", "")
            if coords_str and "," in coords_str:
                try:
                    tx, ty = [int(v.strip()) for v in coords_str.split(",", 1)]
                    _shell(f"uitest uiInput click {tx} {ty}", serial=serial)
                    return True, f"tapped ({tx}, {ty})"
                except ValueError:
                    return False, f"坐标解析失败: {coords_str!r}"
            return False, "coordinates 格式错误，应为 x,y"

        # ── swipe ─────────────────────────────────────────────────────────
        if action == "swipe":
            direction = data.get("value", "up")
            speed = int(data.get("speed", 600))  # px/s，范围 200-40000
            fast = data.get("fast", False)        # True 使用 fling（快滑），False 使用 swipe（慢滑）
            try:
                layout = _dump_layout(serial)
                bounds = layout.get("attributes", {}).get("bounds", "[0,0][1080,2340]")
                m = re.match(r'\[(\d+),(\d+)\]\[(\d+),(\d+)\]', bounds)
                w, h = (int(m.group(3)), int(m.group(4))) if m else (1080, 2340)
            except Exception:
                w, h = 1080, 2340
            cx_s, cy_s = w // 2, h // 2
            coords_map = {
                "up":    (cx_s, int(h * 0.7), cx_s, int(h * 0.3)),
                "down":  (cx_s, int(h * 0.3), cx_s, int(h * 0.7)),
                "left":  (int(w * 0.7), cy_s, int(w * 0.3), cy_s),
                "right": (int(w * 0.3), cy_s, int(w * 0.7), cy_s),
            }
            x1, y1, x2, y2 = coords_map.get(direction, coords_map["up"])
            cmd = "fling" if fast else "swipe"
            _shell(f"uitest uiInput {cmd} {x1} {y1} {x2} {y2} {speed}", serial=serial)
            return True, f"{cmd}d {direction}"

        # ── fling（快滑，按坐标）─────────────────────────────────────────
        if action == "fling":
            fx1 = int(data.get("from_x", 0))
            fy1 = int(data.get("from_y", 0))
            fx2 = int(data.get("to_x", 0))
            fy2 = int(data.get("to_y", 0))
            speed = int(data.get("speed", 600))
            _shell(f"uitest uiInput fling {fx1} {fy1} {fx2} {fy2} {speed}", serial=serial)
            return True, f"flung ({fx1},{fy1}) → ({fx2},{fy2})"

        # ── drag ──────────────────────────────────────────────────────────
        if action == "drag":
            dx1 = int(data.get("from_x", x))
            dy1 = int(data.get("from_y", y))
            dx2 = int(data.get("to_x", 0))
            dy2 = int(data.get("to_y", 0))
            speed = int(data.get("speed", 600))
            _shell(f"uitest uiInput drag {dx1} {dy1} {dx2} {dy2} {speed}", serial=serial)
            return True, f"dragged ({dx1},{dy1}) → ({dx2},{dy2})"

        # ── screenshot ────────────────────────────────────────────────────
        if action == "screenshot":
            # 使用 snapshot_display（性能远优于 uitest screenCap）
            remote = "/data/local/tmp/_agent_shot.jpeg"
            _shell(f"snapshot_display -f {remote}", serial=serial)
            local = f"/tmp/screenshot_{int(time.time())}.jpeg"
            _run(["file", "recv", remote, local], serial=serial)
            return True, f"screenshot saved: {local}"

        # ── press_key ─────────────────────────────────────────────────────
        if action == "press_key":
            key = data.get("key_code", "home")
            key_arg = _KEY_MAP.get(key, key)
            _shell(f"uitest uiInput keyEvent {key_arg}", serial=serial)
            return True, f"pressed key: {key}"

        # ── coords 必须有效才能继续 ───────────────────────────────────────
        if coords is None:
            return False, f"动作 {action!r} 需要元素坐标，但 locator 未提供"

        # ── click ─────────────────────────────────────────────────────────
        if action == "click":
            _shell(f"uitest uiInput click {x} {y}", serial=serial)
            return True, f"clicked ({x},{y})"

        # ── double_click ──────────────────────────────────────────────────
        if action == "double_click":
            _shell(f"uitest uiInput doubleClick {x} {y}", serial=serial)
            return True, f"double_clicked ({x},{y})"

        # ── long_press ────────────────────────────────────────────────────
        if action == "long_press":
            _shell(f"uitest uiInput longClick {x} {y}", serial=serial)
            return True, f"long_pressed ({x},{y})"

        # ── type ──────────────────────────────────────────────────────────
        if action == "type":
            value = data.get("value", "")
            _shell(f"uitest uiInput inputText {x} {y} {value}", serial=serial)
            return True, f"typed {value!r} into ({x},{y})"

        # ── clear_text ────────────────────────────────────────────────────
        if action == "clear_text":
            _shell(f"uitest uiInput click {x} {y}", serial=serial)
            time.sleep(0.2)
            _shell("uitest uiInput keyEvent 2072 2038", serial=serial)  # Ctrl+A（全选）
            time.sleep(0.1)
            _shell("uitest uiInput keyEvent 2055", serial=serial)        # Delete
            return True, f"cleared text at ({x},{y})"

        # ── wait_element ──────────────────────────────────────────────────
        if action == "wait_element":
            return True, f"element appeared at ({x},{y})"

        # ── get_text ──────────────────────────────────────────────────────
        if action == "get_text":
            var_name = data.get("var_name", "result")
            ocr_text = data.get("_ocr_text")
            if ocr_text is not None:
                return True, f"{var_name} = {ocr_text!r}"
            layout = _dump_layout(serial)

            def _walk_text(node: dict) -> str | None:
                bounds = node.get("attributes", {}).get("bounds", "")
                m = re.match(r'\[(\d+),(\d+)\]\[(\d+),(\d+)\]', bounds)
                if m:
                    x1e, y1e, x2e, y2e = map(int, m.groups())
                    if (x1e + x2e) // 2 == x and (y1e + y2e) // 2 == y:
                        return node.get("attributes", {}).get("text", "")
                for child in node.get("children", []):
                    t = _walk_text(child)
                    if t is not None:
                        return t
                return None

            text = _walk_text(layout) or ""
            return True, f"{var_name} = {text!r}"

        return False, f"不支持的 action: {action}"

    except Exception as exc:
        return False, str(exc)


# ── 截图 API ──────────────────────────────────────────────────────────────────

async def capture_screenshot(serial: str | None = None) -> tuple[bytes, int, int]:
    """返回 (img_bytes, device_width, device_height)。"""
    return await asyncio.get_event_loop().run_in_executor(
        None, _capture_screenshot_sync, serial
    )


def _capture_screenshot_sync(serial: str | None) -> tuple[bytes, int, int]:
    """
    使用 snapshot_display 截图（性能远优于 uitest screenCap）。
    返回 (jpeg_bytes, width, height)；宽高从设备屏幕信息获取。
    """
    remote = "/data/local/tmp/_agent_shot.jpeg"
    # 【推荐】hdc.md 方式二：snapshot_display -f，性能远优于 uitest screenCap
    _shell(f"snapshot_display -f {remote}", serial=serial)
    with tempfile.NamedTemporaryFile(suffix=".jpeg", delete=False) as f:
        local = f.name
    try:
        _run(["file", "recv", remote, local], serial=serial)
        with open(local, "rb") as f:
            img_bytes = f.read()
    finally:
        os.unlink(local)

    # 从 JPEG SOF0/SOF2 标记解析宽高
    w, h = _parse_jpeg_size(img_bytes)
    return img_bytes, w, h


def _parse_jpeg_size(data: bytes) -> tuple[int, int]:
    """从 JPEG 字节流解析图片宽高，解析失败返回 (0, 0)。"""
    try:
        i = 2  # 跳过 SOI (FF D8)
        while i < len(data) - 1:
            if data[i] != 0xFF:
                break
            marker = data[i + 1]
            if marker in (0xC0, 0xC1, 0xC2):  # SOF0 / SOF1 / SOF2
                h = struct.unpack(">H", data[i + 5:i + 7])[0]
                w = struct.unpack(">H", data[i + 7:i + 9])[0]
                return w, h
            length = struct.unpack(">H", data[i + 2:i + 4])[0]
            i += 2 + length
    except Exception:
        pass
    return 0, 0


# ── 布局树查找 ────────────────────────────────────────────────────────────────

def _dump_layout(serial: str | None) -> dict:
    """获取鸿蒙 UI 布局树 JSON。"""
    remote = "/data/local/tmp/_agent_layout.json"
    _shell(f"uitest dumpLayout -p {remote}", serial=serial)
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        local = f.name
    try:
        _run(["file", "recv", remote, local], serial=serial)
        with open(local, "r", encoding="utf-8") as f:
            return json.load(f)
    finally:
        os.unlink(local)


def _find_element(serial: str | None, selector: str) -> tuple[int, int]:
    """
    在鸿蒙布局树中查找元素，返回中心坐标 (x, y)。

    支持格式：
    - id:        #loginBtn  或  loginBtn（以 # 开头或不含特殊字符）
    - text:      纯文本（匹配 text / description 属性）
    - type:      Button / TextInput / ...（匹配 type 属性）
    """
    layout = _dump_layout(serial)

    def _walk(node: dict) -> dict | None:
        attrs = node.get("attributes", {})

        # id 匹配
        if selector.startswith("#"):
            sid = selector[1:]
            if attrs.get("id") == sid or attrs.get("key") == sid:
                return node
        elif re.match(r'^[A-Za-z_][A-Za-z0-9_]*$', selector):
            # 纯标识符：先尝试 id/key，再尝试 type
            if attrs.get("id") == selector or attrs.get("key") == selector:
                return node
            if attrs.get("type", "").endswith(selector):
                return node

        # text / description 匹配
        if attrs.get("text") == selector or attrs.get("description") == selector:
            return node

        for child in node.get("children", []):
            found = _walk(child)
            if found:
                return found
        return None

    node = _walk(layout)
    if node is None:
        raise ValueError(f"未找到元素: {selector!r}")

    bounds = node.get("attributes", {}).get("bounds", "")
    m = re.match(r'\[(\d+),(\d+)\]\[(\d+),(\d+)\]', bounds)
    if not m:
        raise ValueError(f"无效的 bounds: {bounds!r}")
    x1, y1, x2, y2 = map(int, m.groups())
    return (x1 + x2) // 2, (y1 + y2) // 2


# ── 按键映射 ──────────────────────────────────────────────────────────────────
# 参考：https://docs.openharmony.cn/pages/v4.1/en/application-dev/reference/apis-input-kit/js-apis-keycode.md

_KEY_MAP = {
    "home":        "Home",
    "back":        "Back",
    "recent":      "2049",  # KEYCODE_RECENT_APPS
    "volume_up":   "16",    # KEYCODE_VOLUME_UP
    "volume_down": "17",    # KEYCODE_VOLUME_DOWN
    "power":       "18",    # KEYCODE_POWER
    "enter":       "23",    # KEYCODE_ENTER
    "delete":      "2055",  # KEYCODE_DEL
    "space":       "2062",  # KEYCODE_SPACE
    "tab":         "2049",  # KEYCODE_TAB
}


# ── 主执行函数 ─────────────────────────────────────────────────────────────────

def _execute(action: str, data: dict, serial: str | None) -> tuple[bool, str]:
    try:
        # ── launch_app ────────────────────────────────────────────────────
        if action == "launch_app":
            app_id = data.get("app_id", "")
            if not app_id:
                return False, "app_id 不能为空"
            # 支持 bundleName 或 bundleName/abilityName 两种格式
            bundle, ability = (app_id.split("/", 1) if "/" in app_id
                               else (app_id, "EntryAbility"))
            launch_type = data.get("launch_type", "cold")
            if launch_type == "cold":
                # 冷起：先强制关闭再启动
                try:
                    _shell(f"aa force-stop {bundle}", serial=serial)
                except Exception:
                    pass
            # hdc.md: aa start -a {abilityName} -b {bundleName}
            _shell(f"aa start -a {ability} -b {bundle}", serial=serial)
            return True, f"已{'冷' if launch_type == 'cold' else '热'}启动 {app_id}"

        # ── stop_app ──────────────────────────────────────────────────────
        if action == "stop_app":
            app_id = data.get("app_id", "")
            if not app_id:
                return False, "app_id 不能为空"
            bundle = app_id.split("/", 1)[0]
            _shell(f"aa force-stop {bundle}", serial=serial)
            return True, f"已退出 {bundle}"

        # ── tap_xy ────────────────────────────────────────────────────────
        if action == "tap_xy":
            coords = data.get("coordinates", "")
            if not coords or "," not in coords:
                return False, "coordinates 格式错误，应为 x,y"
            try:
                x, y = [int(v.strip()) for v in coords.split(",", 1)]
            except ValueError:
                return False, f"坐标解析失败: {coords!r}"
            _shell(f"uitest uiInput click {x} {y}", serial=serial)
            return True, f"tapped ({x}, {y})"

        # ── swipe ─────────────────────────────────────────────────────────
        if action == "swipe":
            direction = data.get("value", "up")
            speed = int(data.get("speed", 600))   # px/s，范围 200-40000
            fast = data.get("fast", False)         # True → fling（快滑），False → swipe（慢滑）
            try:
                layout = _dump_layout(serial)
                bounds = layout.get("attributes", {}).get("bounds", "[0,0][1080,2340]")
                m = re.match(r'\[(\d+),(\d+)\]\[(\d+),(\d+)\]', bounds)
                if m:
                    w, h = int(m.group(3)), int(m.group(4))
                else:
                    w, h = 1080, 2340
            except Exception:
                w, h = 1080, 2340
            cx, cy = w // 2, h // 2
            coords_map = {
                "up":    (cx, int(h * 0.7), cx, int(h * 0.3)),
                "down":  (cx, int(h * 0.3), cx, int(h * 0.7)),
                "left":  (int(w * 0.7), cy, int(w * 0.3), cy),
                "right": (int(w * 0.3), cy, int(w * 0.7), cy),
            }
            x1, y1, x2, y2 = coords_map.get(direction, coords_map["up"])
            cmd = "fling" if fast else "swipe"
            _shell(f"uitest uiInput {cmd} {x1} {y1} {x2} {y2} {speed}", serial=serial)
            return True, f"{cmd}d {direction}"

        # ── fling（快滑，按坐标）─────────────────────────────────────────
        if action == "fling":
            fx1 = int(data.get("from_x", 0))
            fy1 = int(data.get("from_y", 0))
            fx2 = int(data.get("to_x", 0))
            fy2 = int(data.get("to_y", 0))
            speed = int(data.get("speed", 600))
            _shell(f"uitest uiInput fling {fx1} {fy1} {fx2} {fy2} {speed}", serial=serial)
            return True, f"flung ({fx1},{fy1}) → ({fx2},{fy2})"

        # ── drag ──────────────────────────────────────────────────────────
        if action == "drag":
            dx1 = int(data.get("from_x", 0))
            dy1 = int(data.get("from_y", 0))
            dx2 = int(data.get("to_x", 0))
            dy2 = int(data.get("to_y", 0))
            speed = int(data.get("speed", 600))
            _shell(f"uitest uiInput drag {dx1} {dy1} {dx2} {dy2} {speed}", serial=serial)
            return True, f"dragged ({dx1},{dy1}) → ({dx2},{dy2})"

        # ── screenshot ────────────────────────────────────────────────────
        if action == "screenshot":
            # 使用 snapshot_display（性能远优于 uitest screenCap）
            remote = "/data/local/tmp/_agent_shot.jpeg"
            _shell(f"snapshot_display -f {remote}", serial=serial)
            local = f"/tmp/screenshot_{int(time.time())}.jpeg"
            _run(["file", "recv", remote, local], serial=serial)
            return True, f"screenshot saved: {local}"

        # ── press_key ─────────────────────────────────────────────────────
        if action == "press_key":
            key = data.get("key_code", "home")
            key_arg = _KEY_MAP.get(key, key)
            _shell(f"uitest uiInput keyEvent {key_arg}", serial=serial)
            return True, f"pressed key: {key}"

        # ── selector-based actions ────────────────────────────────────────
        selector = data.get("selector", "")
        if not selector:
            return False, "selector 不能为空"

        if action == "click":
            x, y = _find_element(serial, selector)
            _shell(f"uitest uiInput click {x} {y}", serial=serial)
            return True, f"clicked {selector} at ({x},{y})"

        if action == "double_click":
            x, y = _find_element(serial, selector)
            _shell(f"uitest uiInput doubleClick {x} {y}", serial=serial)
            return True, f"double_clicked {selector} at ({x},{y})"

        if action == "long_press":
            x, y = _find_element(serial, selector)
            ms = int(data.get("duration_ms", 1000))
            _shell(f"uitest uiInput longClick {x} {y}", serial=serial)
            return True, f"long_pressed {selector} for {ms}ms"

        if action == "type":
            value = data.get("value", "")
            x, y = _find_element(serial, selector)
            _shell(f"uitest uiInput inputText {x} {y} {value}", serial=serial)
            return True, f"typed {value!r} into {selector}"

        if action == "clear_text":
            x, y = _find_element(serial, selector)
            # 先点击聚焦，再全选删除
            _shell(f"uitest uiInput click {x} {y}", serial=serial)
            time.sleep(0.2)
            _shell("uitest uiInput keyEvent 2072 2038", serial=serial)  # Ctrl+A（全选）
            time.sleep(0.1)
            _shell("uitest uiInput keyEvent 2055", serial=serial)        # Delete
            return True, f"cleared text in {selector}"

        if action == "wait_element":
            timeout  = 10
            interval = 0.5
            deadline = time.time() + timeout
            while time.time() < deadline:
                try:
                    _find_element(serial, selector)
                    return True, f"element appeared: {selector}"
                except ValueError:
                    time.sleep(interval)
            return False, f"element not found within {timeout}s: {selector}"

        if action == "get_text":
            var_name = data.get("var_name", "result")
            x, y = _find_element(serial, selector)
            layout = _dump_layout(serial)

            def _walk_text(node: dict) -> str | None:
                bounds = node.get("attributes", {}).get("bounds", "")
                m = re.match(r'\[(\d+),(\d+)\]\[(\d+),(\d+)\]', bounds)
                if m:
                    x1, y1, x2, y2 = map(int, m.groups())
                    if (x1 + x2) // 2 == x and (y1 + y2) // 2 == y:
                        return node.get("attributes", {}).get("text", "")
                for child in node.get("children", []):
                    t = _walk_text(child)
                    if t is not None:
                        return t
                return None

            text = _walk_text(layout) or ""
            return True, f"{var_name} = {text!r}"

        return False, f"不支持的 action: {action}"

    except Exception as exc:
        return False, str(exc)
