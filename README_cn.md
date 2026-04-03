# universal-network-access-mcp（通用网络访问 MCP）

一个本地 MCP 服务器，用于突破 Claude 的沙箱网络隔离，使 Claude 可以完全访问你本地机器的网络，包括 MySQL、Redis、SSH、FTP、HTTP API 以及任何你机器可访问的服务。



## MCP Server 架构

```shell
+----------------+        JSON-RPC         +--------------------------+
|    Claude      | <---------------------> |  Universal Local MCP     |
|  (沙箱)        |        over stdio       |  Server (Python 脚本)    |
+----------------+                         +--------------------------+
         |                                           |
         | 调用 run_python / run_shell               |
         |                                           |
         v                                           v
  +----------------+                         +--------------------------+
  | 本地服务       |                         | 本地 Python / 系统命令     |
  | (MySQL, Redis, |                         | 网络访问、pip 安装缺失库、  |
  |  FTP, SSH,     |                         | 文件操作等)               |
  |  HTTP API ...) |                         +--------------------------+
```

**工作原理（文字说明）**

1. Claude 通过 JSON-RPC（标准输入输出）向 MCP 服务器发送请求。
2. MCP 服务器将请求路由到相应工具（`run_python` 或 `run_shell`）。
3. Python 代码在本机执行，具有完整网络访问权限；缺少的模块会自动通过 pip 安装。
4. Shell 命令在本地系统执行，支持 `cmd`、PowerShell 或 bash。
5. MCP 服务器通过 JSON-RPC 将执行结果返回给 Claude。
6. 通过该机制，Claude 可以访问任意本地服务或网络资源，突破沙箱限制。

## 系统要求

* Python 3.8 或更高版本 — [python.org/downloads](https://www.python.org/downloads/)
* Claude Desktop 或 Claude Cowork 桌面应用

## 安装步骤

### 1. 下载脚本

将 `universal-network-access-mcp.py` 保存到本地任意位置。例如：

* **Windows:** `C:\Users\<YourName>\mcp\universal-network-access-mcp.py`
* **macOS / Linux:** `~/mcp/universal-network-access-mcp.py`

### 2. 找到 Claude 配置文件

| 平台      | 应用             | 配置文件路径                                                                                              |
| ------- | -------------- | --------------------------------------------------------------------------------------------------- |
| Windows | Claude Desktop | `%APPDATA%\Claude\claude_desktop_config.json`                                                       |
| Windows | Claude Cowork  | `%LOCALAPPDATA%\Packages\Claude_pzs8sxrjxfjjc\LocalCache\Roaming\Claude\claude_desktop_config.json` |
| macOS   | Claude Desktop | `~/Library/Application Support/Claude/claude_desktop_config.json`                                   |
| macOS   | Claude Cowork  | `~/Library/Application Support/Claude Cowork/claude_desktop_config.json`                            |

> **提示（Windows）:** 按 `Win + R`，粘贴上面的路径，然后回车即可直接打开。

### 3. 编辑配置文件

打开配置文件，添加 `mcpServers` 条目。将路径替换为你保存脚本的位置。

**Windows 示例:**

```json
{
  "mcpServers": {
    "universal-network-access-mcp": {
      "command": "python",
      "args": ["C:\\Users\\<YourName>\\mcp\\universal-network-access-mcp.py"]
    }
  }
}
```

**macOS / Linux 示例:**

```json
{
  "mcpServers": {
    "universal-network-access-mcp": {
      "command": "python3",
      "args": ["/Users/<YourName>/mcp/universal-network-access-mcp.py"]
    }
  }
}
```

> 如果你已经配置了其他 MCP 服务器，只需将 `"universal-network-access-mcp"` 条目添加到现有的 `"mcpServers"` 对象中即可。

### 4. 重启 Claude

完全退出并重新启动 Claude Desktop / Cowork，MCP 服务器将自动启动。

## 使用示例

安装完成后，只需用自然语言描述你的需求，Claude 会自动使用 `run_python` 或 `run_shell`。

**连接本地 MySQL 数据库：**

> "连接 127.0.0.1 上的 MySQL，用户 root，密码 abc123。列出所有数据库。"

**查询 Redis：**

> "连接 127.0.0.1 上的 Redis，密码 xyz。查看 key `session:42` 的值。"

**SSH 登录远程服务器：**

> "SSH 登录 192.168.1.100，用户 root，密码 abc。执行 `df -h`。"

**调用本地 HTTP API：**

> "GET [http://localhost:8080/api/status](http://localhost:8080/api/status) 并显示返回结果。"

**安装并使用 Python 库：**

> "使用 `httpx` 库获取 [https://api.github.com](https://api.github.com) 并打印 rate limit 头信息。"
> *(如果缺少库，Claude 会自动通过 pip 安装)*

## 常见问题排查

**`python: command not found` 或 Windows 打开商店而不是 Python**

脚本会自动检测真实的 Python 可执行路径。如果仍然失败，请在配置中指定完整路径：

```json
"command": "C:\\Users\\<YourName>\\AppData\\Local\\Programs\\Python\\Python312\\python.exe"
```

在 macOS 上使用 `python3` 或 `which python3` 得到的完整路径。

**macOS 权限被拒绝**

```bash
chmod +x ~/mcp/universal-network-access-mcp.py
```

**MCP 服务器未显示在 Claude 中**

* 确认 JSON 文件格式正确（无多余逗号，括号匹配）。
* 确认脚本路径正确且文件存在。
* 完全退出 Claude 并重新启动，不要只关闭窗口。

## 安全说明

该 MCP 允许 Claude 在你的机器上执行任意代码和命令。仅在信任的机器上使用，并仅在你愿意共享的会话中提供密码或 IP 地址。
