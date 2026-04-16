"""
系统服务安装/卸载
- macOS  : ~/Library/LaunchAgents/com.opentest.agent.plist  (launchd)
- Linux  : ~/.config/systemd/user/open-test-agent.service   (systemd --user)
- Windows: 任务计划程序 (schtasks)
"""
import os
import platform
import subprocess
import sys
from pathlib import Path


LABEL = "com.opentest.agent"
SERVICE_NAME = "open-test-agent"
PYTHON = sys.executable


# ---------------------------------------------------------------------------
# macOS
# ---------------------------------------------------------------------------

_MACOS_PLIST_PATH = Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"

_MACOS_PLIST_TEMPLATE = """\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{label}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{python}</string>
        <string>-m</string>
        <string>open_test_agent.main</string>
        <string>agent</string>
        <string>start</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>{log_dir}/open-test-agent.log</string>
    <key>StandardErrorPath</key>
    <string>{log_dir}/open-test-agent-error.log</string>
</dict>
</plist>
"""

# ---------------------------------------------------------------------------
# Linux (systemd --user)
# ---------------------------------------------------------------------------

_LINUX_UNIT_PATH = Path.home() / ".config/systemd/user" / f"{SERVICE_NAME}.service"

_LINUX_UNIT_TEMPLATE = """\
[Unit]
Description=open-test-agent 本地执行 Agent
After=network.target

[Service]
ExecStart={python} -m open_test_agent.main agent start
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
"""

# ---------------------------------------------------------------------------
# Windows (schtasks)
# ---------------------------------------------------------------------------

_WINDOWS_TASK_TEMPLATE = (
    'schtasks /Create /F /SC ONLOGON /DELAY 0000:10 /TN "{name}" '
    '/TR "\\"{python}\\" -m open_test_agent.main agent start" /RL HIGHEST'
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def install() -> str:
    """安装系统服务，开机自启。返回成功/失败消息。"""
    system = platform.system()
    if system == "Darwin":
        return _install_macos()
    if system == "Linux":
        return _install_linux()
    if system == "Windows":
        return _install_windows()
    return f"不支持的系统: {system}"


def uninstall() -> str:
    """卸载系统服务。"""
    system = platform.system()
    if system == "Darwin":
        return _uninstall_macos()
    if system == "Linux":
        return _uninstall_linux()
    if system == "Windows":
        return _uninstall_windows()
    return f"不支持的系统: {system}"


def status() -> str:
    """返回服务当前状态描述。"""
    import httpx
    try:
        r = httpx.get("http://localhost:7357/health", timeout=2)
        data = r.json()
        devices = data.get("devices", [])
        dev_str = f"，已连接设备: {', '.join(devices)}" if devices else "，未检测到 ADB 设备"
        return f"运行中{dev_str}"
    except Exception:
        return "未运行"


# ---------------------------------------------------------------------------
# macOS impl
# ---------------------------------------------------------------------------

def _install_macos() -> str:
    log_dir = Path.home() / "Library/Logs"
    _MACOS_PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    _MACOS_PLIST_PATH.write_text(
        _MACOS_PLIST_TEMPLATE.format(label=LABEL, python=PYTHON, log_dir=log_dir)
    )
    result = subprocess.run(
        ["launchctl", "load", "-w", str(_MACOS_PLIST_PATH)],
        capture_output=True, text=True
    )
    if result.returncode == 0:
        return f"已安装并启动 (macOS LaunchAgent)\n配置文件: {_MACOS_PLIST_PATH}"
    return f"安装失败: {result.stderr.strip()}"


def _uninstall_macos() -> str:
    if _MACOS_PLIST_PATH.exists():
        subprocess.run(["launchctl", "unload", "-w", str(_MACOS_PLIST_PATH)], capture_output=True)
        _MACOS_PLIST_PATH.unlink()
        return "已卸载 (macOS LaunchAgent)"
    return "未安装"


# ---------------------------------------------------------------------------
# Linux impl
# ---------------------------------------------------------------------------

def _install_linux() -> str:
    _LINUX_UNIT_PATH.parent.mkdir(parents=True, exist_ok=True)
    _LINUX_UNIT_PATH.write_text(_LINUX_UNIT_TEMPLATE.format(python=PYTHON))
    subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
    subprocess.run(["systemctl", "--user", "enable", "--now", SERVICE_NAME], capture_output=True)
    return f"已安装并启动 (systemd user service)\n配置文件: {_LINUX_UNIT_PATH}"


def _uninstall_linux() -> str:
    subprocess.run(["systemctl", "--user", "disable", "--now", SERVICE_NAME], capture_output=True)
    if _LINUX_UNIT_PATH.exists():
        _LINUX_UNIT_PATH.unlink()
    subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
    return "已卸载 (systemd user service)"


# ---------------------------------------------------------------------------
# Windows impl
# ---------------------------------------------------------------------------

def _install_windows() -> str:
    cmd = _WINDOWS_TASK_TEMPLATE.format(name=SERVICE_NAME, python=PYTHON)
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if result.returncode == 0:
        # 立即启动一次
        subprocess.Popen([PYTHON, "-m", "open_test_agent.main", "agent", "start"])
        return f"已安装 (Windows 任务计划程序: {SERVICE_NAME})"
    return f"安装失败: {result.stderr.strip()}"


def _uninstall_windows() -> str:
    result = subprocess.run(
        f'schtasks /Delete /F /TN "{SERVICE_NAME}"',
        shell=True, capture_output=True, text=True
    )
    if result.returncode == 0:
        return f"已卸载 (Windows 任务计划程序: {SERVICE_NAME})"
    return f"卸载失败: {result.stderr.strip()}"
