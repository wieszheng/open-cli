"""
ADB / uiautomator2 驱动
封装 appUiAction 节点的实际执行逻辑

STUB 模式：uiautomator2 暂未安装，所有 ADB 操作均模拟执行（固定成功+随机延迟）。
待真机联调时，将 STUB = False 并安装 uiautomator2 即可切换为真实执行。
"""
import asyncio
import subprocess

STUB = True   # 设为 False 启用真实 uiautomator2 执行


def check_adb() -> list[str]:
    """返回已连接设备的序列号列表。STUB 模式下返回虚拟设备。"""
    if STUB:
        return ["stub-device-001"]
    try:
        result = subprocess.run(
            ["adb", "devices"],
            capture_output=True, text=True, timeout=5
        )
        lines = result.stdout.strip().splitlines()[1:]  # 跳过 "List of devices attached"
        return [
            line.split()[0]
            for line in lines
            if line.strip() and "offline" not in line
        ]
    except Exception:
        return []


async def run_app_ui_action(data: dict) -> tuple[bool, str]:
    """执行 appUiAction 节点。STUB 模式下模拟执行。"""
    if STUB:
        return await _stub_action(data)

    try:
        import uiautomator2 as u2  # noqa: F401
    except ImportError:
        return False, "uiautomator2 未安装，请执行: pip install open-test-agent[android]"

    action = data.get("action", "click")
    device_serial = data.get("device_serial") or None
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _execute_u2, action, data, device_serial)


async def _stub_action(data: dict) -> tuple[bool, str]:
    """模拟 ADB 执行，随机延迟 0.3~0.8s 后返回成功。"""
    import random
    action = data.get("action", "click")
    await asyncio.sleep(random.uniform(0.3, 0.8))

    if action == "launch_app":
        app_id = data.get("app_id", "com.example.app")
        return True, f"[stub] launched {app_id}"

    selector = data.get("selector", "<selector>")
    value = data.get("value", "")

    messages = {
        "click":        f"[stub] clicked: {selector}",
        "type":         f"[stub] typed {value!r} into {selector}",
        "swipe":        f"[stub] swiped {value or 'up'}",
        "wait_element": f"[stub] element appeared: {selector}",
        "screenshot":   "[stub] screenshot saved: /tmp/stub_screenshot.png",
    }
    return True, messages.get(action, f"[stub] {action} OK")


def _execute_u2(action: str, data: dict, serial: str | None) -> tuple[bool, str]:
    import uiautomator2 as u2

    d = u2.connect(serial)

    try:
        if action == "launch_app":
            app_id = data.get("app_id", "")
            if not app_id:
                return False, "app_id 不能为空"
            d.app_start(app_id)
            return True, f"已启动 {app_id}"

        selector_str = data.get("selector", "")
        if not selector_str:
            return False, "selector 不能为空"

        if selector_str.startswith("//"):
            elem = d.xpath(selector_str)
        else:
            elem = d(resourceId=selector_str)

        if action == "click":
            elem.click()
            return True, f"clicked: {selector_str}"

        if action == "type":
            value = data.get("value", "")
            elem.set_text(value)
            return True, f"typed: {value!r}"

        if action == "swipe":
            direction = data.get("value", "up")
            d.swipe_ext(direction)
            return True, f"swiped {direction}"

        if action == "wait_element":
            existed = elem.wait(timeout=10)
            if existed:
                return True, f"element appeared: {selector_str}"
            return False, f"element not found within 10s: {selector_str}"

        if action == "screenshot":
            path = f"/tmp/screenshot_{int(__import__('time').time())}.png"
            d.screenshot(path)
            return True, f"screenshot saved: {path}"

        return False, f"不支持的 action: {action}"

    except Exception as exc:
        return False, str(exc)
