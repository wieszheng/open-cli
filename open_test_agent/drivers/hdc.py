"""
HDC 驱动（鸿蒙设备，基于 hdc 命令行）
封装 appUiAction 节点的实际执行逻辑

依赖：hdc 命令行工具（随 DevEco Studio / OpenHarmony SDK 安装）
  - 触控注入：uitest uiInput click/swipe/longClick/doubleClick/keyEvent/inputText
  - 截图：     uitest screenCap + hdc file recv
  - 布局树：   uitest dumpLayout + hdc file recv
  - 应用启动： aa start -b <bundleName> -a <abilityName>
"""
import asyncio
import json
import os
import re
import shutil
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


# ── 截图 API ──────────────────────────────────────────────────────────────────

async def capture_screenshot(serial: str | None = None) -> tuple[bytes, int, int]:
    """返回 (png_bytes, device_width, device_height)。"""
    return await asyncio.get_event_loop().run_in_executor(
        None, _capture_screenshot_sync, serial
    )


def _capture_screenshot_sync(serial: str | None) -> tuple[bytes, int, int]:
    remote = "/data/local/tmp/_agent_shot.png"
    _shell(f"uitest screenCap -p {remote}", serial=serial)
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        local = f.name
    try:
        _run(["file", "recv", remote, local], serial=serial)
        with open(local, "rb") as f:
            png_bytes = f.read()
    finally:
        os.unlink(local)

    # 从 PNG IHDR 块读取宽高（偏移 16-23 字节）
    import struct
    w = struct.unpack(">I", png_bytes[16:20])[0]
    h = struct.unpack(">I", png_bytes[20:24])[0]
    return png_bytes, w, h


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

_KEY_MAP = {
    "home":        "Home",
    "back":        "Back",
    "recent":      "2049",  # KEYCODE_RECENT_APPS
    "volume_up":   "16",    # KEYCODE_VOLUME_UP
    "volume_down": "17",    # KEYCODE_VOLUME_DOWN
    "power":       "18",    # KEYCODE_POWER
    "enter":       "23",    # KEYCODE_ENTER
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
            if "/" in app_id:
                bundle, ability = app_id.split("/", 1)
            else:
                bundle, ability = app_id, "MainAbility"
            _shell(f"aa start -b {bundle} -a {ability}", serial=serial)
            return True, f"已启动 {app_id}"

        # ── tap_xy ────────────────────────────────────────────────────────
        if action == "tap_xy":
            coords = data.get("coordinates", "")
            if not coords or "," not in coords:
                return False, "coordinates 格式错误，应为 x,y"
            try:
                x, y = [int(v.strip()) for v in coords.split(",", 1)]
            except ValueError:
                return False, f"坐标解析失败: {coords!r}"
            out = _shell(f"uitest uiInput click {x} {y}", serial=serial)
            return True, f"tapped ({x}, {y})"

        # ── swipe ─────────────────────────────────────────────────────────
        if action == "swipe":
            direction = data.get("value", "up")
            # 获取屏幕尺寸（从 layout 根节点 bounds）
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
            _shell(f"uitest uiInput swipe {x1} {y1} {x2} {y2} 600", serial=serial)
            return True, f"swiped {direction}"

        # ── screenshot ────────────────────────────────────────────────────
        if action == "screenshot":
            remote = "/data/local/tmp/_agent_shot.png"
            _shell(f"uitest screenCap -p {remote}", serial=serial)
            local = f"/tmp/screenshot_{int(time.time())}.png"
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
            _shell("uitest uiInput keyEvent 2072 2", serial=serial)   # Ctrl+A
            time.sleep(0.1)
            _shell("uitest uiInput keyEvent 2055", serial=serial)      # Delete
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
