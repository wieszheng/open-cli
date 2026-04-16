# open-test-agent CLI

本地工作流执行 + Android App UI 自动化代理，配合 open-test 平台使用。

## 安装

```bash
cd cli
pip install -e .                    # 开发安装（可编辑）
# 或
pip install open-test-agent         # 从 PyPI 安装（发布后）
```

Android 真机支持（可选）：

```bash
pip install open-test-agent[android]
```

## 命令

### 执行工作流

```bash
# 从服务器拉取指定用例的工作流并执行
open-test run --case-id 1

# 直接执行本地 JSON 文件
open-test run --file example_flow.json

# 指定后端地址
open-test run --case-id 1 --server http://192.168.1.10:8000
```

### 管理本地 Agent 服务

```bash
open-test agent install      # 安装为系统服务（开机自启）
open-test agent uninstall    # 卸载系统服务
open-test agent status       # 查看运行状态 + 已连接设备
open-test agent start        # 前台启动（调试用，Ctrl+C 退出）
```

## 本地 Agent

浏览器执行工作流时，`appUiAction` 节点会委派给本地 Agent 执行（需连接 Android 设备）。

**安装后 Agent 自动在后台运行，无需手动干预。**

### Agent HTTP 接口（localhost:7357）

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/health` | 状态 + 已连接设备列表 |
| POST | `/execute` | 执行单个节点 |
| GET | `/logs` | 查看最近执行日志（`?limit=N`） |
| GET | `/logs/stream` | SSE 实时尾随日志 |

实时查看执行日志：

```bash
curl -N http://localhost:7357/logs/stream
```

### 切换真实 ADB 执行

当前默认为 **Stub 模式**（模拟执行，无需真机）。切换步骤：

1. 连接 Android 设备（USB 或 WiFi ADB），确认 `adb devices` 可见
2. 安装驱动：`pip install open-test-agent[android]`
3. 编辑 `open_test_agent/drivers/adb.py`，将第 13 行改为：
   ```python
   STUB = False
   ```
4. 重启 Agent：`open-test agent start`

## 支持的 App UI 操作（appUiAction）

| action | 必填参数 | 说明 |
|--------|---------|------|
| `launch_app` | `app_id` | 启动 App，如 `com.example.app` |
| `click` | `selector` | 点击组件 |
| `type` | `selector`, `value` | 输入文本 |
| `swipe` | `value` | 滑动：`up / down / left / right` |
| `wait_element` | `selector` | 等待组件出现（10s 超时） |
| `screenshot` | — | 截图，保存到 `/tmp/` |

**selector 格式：**

```
XPath:       //android.widget.Button[@text='登录']
Resource ID: com.example.app:id/btn_login
```

## 工作流 JSON 格式

参考 [example_flow.json](example_flow.json)，节点结构：

```jsonc
{
  "nodes": [
    {
      "id": "step_1",
      "type": "appUiAction",
      "position": {"x": 100, "y": 100},
      "data": {
        "label": "点击登录按钮",
        "action": "click",
        "selector": "//android.widget.Button[@text='登录']"
      }
    }
  ],
  "edges": [
    {"id": "e1-2", "source": "step_1", "target": "step_2"}
  ]
}
```

支持的节点类型：`httpRequest` / `assertion` / `extract` / `wait` / `condition` / `script` / `sqlQuery` / `webUiAction` / `appUiAction`

## 系统服务说明

`open-test agent install` 根据当前操作系统自动选择服务类型：

| 系统 | 类型 | 配置文件 |
|------|------|---------|
| macOS | launchd LaunchAgent | `~/Library/LaunchAgents/com.opentest.agent.plist` |
| Linux | systemd user service | `~/.config/systemd/user/open-test-agent.service` |
| Windows | 任务计划程序 | 任务名 `open-test-agent` |

**macOS 日志：**

```bash
tail -f ~/Library/Logs/open-test-agent.log
tail -f ~/Library/Logs/open-test-agent-error.log
```

**Linux 日志：**

```bash
journalctl --user -u open-test-agent -f
```

## 项目结构

```
cli/
├── pyproject.toml
└── open_test_agent/
    ├── main.py           # CLI 入口（click）
    ├── executor.py       # 本地工作流执行引擎（open-test run 使用）
    ├── agent_server.py   # 本地 Agent HTTP 服务（FastAPI :7357）
    ├── install.py        # 系统服务安装/卸载
    └── drivers/
        └── adb.py        # ADB / uiautomator2 驱动（含 Stub 模式）
```

## 依赖

| 包 | 用途 |
|----|------|
| click | CLI 框架 |
| rich | 终端彩色输出 |
| httpx | HTTP 客户端 |
| fastapi + uvicorn | Agent HTTP 服务 |
| jsonpath-ng | JSONPath 变量提取 |
| uiautomator2 *(可选)* | Android UI 自动化 |
