# universal-network-access-mcp

<p align="center">
  Languages:
  <a href="./README.md">English</a> ·
  <a href="./README_cn.md">中文</a> ·
</p>

A local MCP server that breaks through Claude's sandbox network isolation, giving Claude full access to your local machine's network — MySQL, Redis, SSH, FTP, HTTP APIs, and anything else reachable from your machine.



## MCP Server Architecture

```
+----------------+        JSON-RPC         +--------------------------+
|    Claude      | <---------------------> |  Universal Local MCP     |
|  (Sandbox)     |        over stdio       |  Server (Python Script)  |
+----------------+                         +--------------------------+
         |                                           |
         | Calls run_python / run_shell              |
         |                                           |
         v                                           v
  +----------------+                         +--------------------------+
  | Local Services |                         | Local Python / System     |
  | (MySQL, Redis, |                         | Commands, Network Access, |
  |  FTP, SSH,     |                         | pip install missing libs, |
  |  HTTP API ...) |                         | file operations, etc.     |
  +----------------+                         +--------------------------+
```

**How it works (textual explanation)**

1. Claude sends a request to the MCP server via JSON-RPC over stdio.
2. MCP server routes the request to the appropriate tool (`run_python` or `run_shell`).
3. Python code is executed with full network access; missing modules are automatically installed via pip.
4. Shell commands are executed on the local system, supporting `cmd`, PowerShell, or bash.
5. MCP server returns results back to Claude via JSON-RPC.
6. Through this mechanism, Claude can access any local service or network resource, bypassing the sandbox.



## Requirements

- Python 3.8 or later — [python.org/downloads](https://www.python.org/downloads/)
- Claude Desktop or Claude Cowork desktop app

## Installation

### 1. Download the script

Save `universal-network-access-mcp.py` anywhere on your machine. For example:

- **Windows:** `C:\Users\<YourName>\mcp\universal-network-access-mcp.py`
- **macOS / Linux:** `~/mcp/universal-network-access-mcp.py`

### 2. Find your Claude config file

| Platform | App | Config file path |
|----------|-----|-----------------|
| Windows | Claude Desktop | `%APPDATA%\Claude\claude_desktop_config.json` |
| Windows | Claude Cowork | `%LOCALAPPDATA%\Packages\Claude_pzs8sxrjxfjjc\LocalCache\Roaming\Claude\claude_desktop_config.json` |
| macOS | Claude Desktop | `~/Library/Application Support/Claude/claude_desktop_config.json` |
| macOS | Claude Cowork | `~/Library/Application Support/Claude Cowork/claude_desktop_config.json` |

> **Tip (Windows):** Press `Win + R`, paste the path above, and press Enter to open it directly.

### 3. Edit the config file

Open the config file and add the `mcpServers` entry. Replace the path with wherever you saved the script.

**Windows example:**
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

**macOS / Linux example:**
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

> If you have other MCP servers already configured, just add the `"universal-network-access-mcp"` entry inside the existing `"mcpServers"` object.

### 4. Restart Claude

Fully quit and relaunch Claude Desktop / Cowork. The MCP server will start automatically.

## Usage examples

Once installed, just describe what you want in plain language. Claude will use `run_python` or `run_shell` automatically.

**Connect to a local MySQL database:**
> "Connect to MySQL at 127.0.0.1, user root, password abc123. List all databases."

**Query Redis:**
> "Connect to Redis at 127.0.0.1 with password xyz. What's the value of key `session:42`?"

**SSH into a remote server:**
> "SSH into 192.168.1.100 as root with password abc. Run `df -h`."

**Call a local HTTP API:**
> "GET http://localhost:8080/api/status and show me the response."

**Install a package and use it:**
> "Use the `httpx` library to fetch https://api.github.com and print the rate limit headers."
> *(Claude will pip install httpx automatically if not present)*

## Troubleshooting

**`python: command not found` or Windows Store opens instead of Python**

The script auto-detects the real Python executable. If it still fails, specify the full path in the config:

```json
"command": "C:\\Users\\<YourName>\\AppData\\Local\\Programs\\Python\\Python312\\python.exe"
```

On macOS, use `python3` or the full path from `which python3`.

**Permission denied on macOS**

Make the script executable:
```bash
chmod +x ~/mcp/universal-network-access-mcp.py
```

**MCP server not showing up in Claude**

- Make sure the JSON in the config file is valid (no trailing commas, matched brackets).
- Check that the path to the script is correct and the file exists.
- Fully quit Claude (not just close the window) and relaunch.

## Security note

This MCP gives Claude the ability to run arbitrary code and commands on your machine. Only use it on machines you trust, and only share connections (passwords, IPs) in conversations you're comfortable with.
