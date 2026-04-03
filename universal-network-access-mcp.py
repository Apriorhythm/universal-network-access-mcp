"""
Universal Local MCP Server v2.1
打通 Claude Cowork 沙箱网络隔离，让 Claude 可以访问本机任意服务。

改进：
  - run_python 自动检测缺少的包并 pip install 后重试
  - 自动定位真实 Python 路径，避免 Windows Store 占位符问题

工具：
  run_python — 执行任意 Python 代码（可 import 任何已装库，完整网络访问）
  run_shell  — 执行任意系统命令（cmd / PowerShell / bash）
"""

import json
import sys
import os
import re
import subprocess


# ============================================================
# 找到真实的 Python 可执行文件路径
# 避免 Windows Store 的占位符 python.exe（会弹出应用商店或报错）
# ============================================================
def _find_real_python() -> str:
    current = sys.executable

    # 如果当前解释器就是真实的（不在 WindowsApps 里），直接用
    if "WindowsApps" not in current:
        return current

    # 尝试常见安装路径
    candidates = [
        # Python 官方安装器（用户级）
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\Python"),
        # Python 官方安装器（系统级）
        r"C:\Python3*",
        r"C:\Python*",
        # %LOCALAPPDATA%\Python 下的各版本
        os.path.expandvars(r"%LOCALAPPDATA%\Python"),
        # conda / miniforge
        os.path.expandvars(r"%USERPROFILE%\miniconda3"),
        os.path.expandvars(r"%USERPROFILE%\anaconda3"),
        os.path.expandvars(r"%USERPROFILE%\miniforge3"),
    ]

    import glob
    for pattern in candidates:
        for path in glob.glob(pattern):
            for sub in ["python.exe", r"bin\python.exe", r"Scripts\python.exe"]:
                full = os.path.join(path, sub)
                if os.path.isfile(full) and "WindowsApps" not in full:
                    return full
            # 子目录（如 pythoncore-3.14-64）
            for sub_dir in glob.glob(os.path.join(path, "*")):
                candidate = os.path.join(sub_dir, "python.exe")
                if os.path.isfile(candidate) and "WindowsApps" not in candidate:
                    return candidate

    # fallback：用 where 命令找第一个非 WindowsApps 的 python
    try:
        result = subprocess.run(
            ["where", "python"], capture_output=True, text=True
        )
        for line in result.stdout.strip().splitlines():
            line = line.strip()
            if line and "WindowsApps" not in line and os.path.isfile(line):
                return line
    except Exception:
        pass

    # 实在找不到，返回 sys.executable（可能不好用但总比没有强）
    return current


PYTHON = _find_real_python()


# ============================================================
# 工具实现
# ============================================================

def _extract_missing_module(stderr: str) -> str | None:
    """从 ModuleNotFoundError 的 stderr 中提取缺失的模块名"""
    m = re.search(r"No module named '([^']+)'", stderr)
    if m:
        # 取顶层包名（e.g. 'paramiko.transport' -> 'paramiko'）
        return m.group(1).split(".")[0]
    return None


def _run_code(code: str, timeout: int) -> tuple[int, str, str]:
    """执行 Python 代码，返回 (returncode, stdout, stderr)"""
    proc = subprocess.run(
        [PYTHON, "-c", code],
        capture_output=True,
        timeout=timeout,
        encoding="utf-8",
        errors="replace",
    )
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def tool_run_python(args: dict) -> str:
    code = args.get("code", "")
    timeout = int(args.get("timeout", 30))
    if not code:
        return "请提供 code 参数"

    try:
        rc, out, err = _run_code(code, timeout)

        # 自动处理缺包：检测到 ModuleNotFoundError 就 pip install 后重试（最多 3 次）
        for _ in range(3):
            if rc != 0 and "ModuleNotFoundError" in err:
                module = _extract_missing_module(err)
                if not module:
                    break
                # 自动安装
                install = subprocess.run(
                    [PYTHON, "-m", "pip", "install", module, "-q"],
                    capture_output=True, text=True, timeout=60,
                )
                if install.returncode != 0:
                    return f"自动安装 {module} 失败：\n{install.stderr}"
                # 重试
                rc, out, err = _run_code(code, timeout)
            else:
                break

        result = f"退出码：{rc}"
        if out:
            result += f"\n\n输出：\n{out[:8000]}"
        if err:
            result += f"\n\n错误：\n{err[:3000]}"
        return result or "（执行完毕，无输出）"

    except subprocess.TimeoutExpired:
        return f"执行超时（{timeout}s）"
    except Exception as e:
        return f"执行失败：{e}"


def tool_run_shell(args: dict) -> str:
    command = args.get("command", "")
    shell_type = args.get("shell", "cmd")
    timeout = int(args.get("timeout", 30))
    if not command:
        return "请提供 command 参数"

    try:
        if shell_type == "powershell":
            proc = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
                capture_output=True, text=True, timeout=timeout,
                encoding="utf-8", errors="replace",
            )
            out, err = proc.stdout or "", proc.stderr or ""
        elif shell_type == "bash":
            proc = subprocess.run(
                ["bash", "-c", command],
                capture_output=True, text=True, timeout=timeout,
                encoding="utf-8", errors="replace",
            )
            out, err = proc.stdout or "", proc.stderr or ""
        else:  # cmd 默认
            proc = subprocess.run(
                command, shell=True, capture_output=True, timeout=timeout,
            )
            out = proc.stdout.decode("gbk", errors="replace") if proc.stdout else ""
            err = proc.stderr.decode("gbk", errors="replace") if proc.stderr else ""

        result = f"退出码：{proc.returncode}"
        if out: result += f"\n\nSTDOUT:\n{out[:8000]}"
        if err: result += f"\n\nSTDERR:\n{err[:3000]}"
        return result or "（执行完毕，无输出）"

    except subprocess.TimeoutExpired:
        return f"命令超时（{timeout}s）"
    except Exception as e:
        return f"执行失败：{e}"


# ============================================================
# 工具定义（MCP Schema）
# ============================================================

TOOLS = [
    {
        "name": "run_python",
        "description": (
            "在用户本机执行任意 Python 代码，具有完整网络访问能力，可突破 Claude 沙箱限制。"
            "自动检测并安装缺少的第三方库（ModuleNotFoundError 时自动 pip install 并重试）。"
            "可 import 任何库：requests/pymysql/redis/paramiko/ftplib/smtplib/pymongo 等。"
            "适合：连接 MySQL/Redis/FTP/SSH/HTTP/SMTP 等任意网络服务，数据处理，文件操作。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "code":    {"type": "string",  "description": "完整 Python 代码，用 print() 输出结果"},
                "timeout": {"type": "integer", "description": "超时秒数，默认 30", "default": 30},
            },
            "required": ["code"],
        },
    },
    {
        "name": "run_shell",
        "description": (
            "在用户本机执行系统命令，支持 cmd（默认）/ PowerShell / bash。"
            "用途：pip install 安装依赖、curl/ssh/ftp 命令、查看网络/进程/文件、管理服务等。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "要执行的命令"},
                "shell":   {
                    "type": "string",
                    "description": "Shell 类型：cmd（默认）/ powershell / bash",
                    "enum": ["cmd", "powershell", "bash"],
                    "default": "cmd"
                },
                "timeout": {"type": "integer", "description": "超时秒数，默认 30", "default": 30},
            },
            "required": ["command"],
        },
    },
]

HANDLERS = {
    "run_python": tool_run_python,
    "run_shell":  tool_run_shell,
}


# ============================================================
# MCP 协议（JSON-RPC over stdio）
# ============================================================

def handle(req: dict):
    method = req.get("method")
    rid    = req.get("id")
    params = req.get("params", {})

    ok  = lambda r: {"jsonrpc": "2.0", "id": rid, "result": r}
    err = lambda c, m: {"jsonrpc": "2.0", "id": rid, "error": {"code": c, "message": m}}

    if method == "initialize":
        return ok({
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "universal-local-mcp", "version": "2.1.0"},
        })
    elif method == "tools/list":
        return ok({"tools": TOOLS})
    elif method == "tools/call":
        name    = params.get("name")
        handler = HANDLERS.get(name)
        if not handler:
            return err(-32601, f"未知工具: {name}")
        try:
            text = handler(params.get("arguments", {}))
            return ok({"content": [{"type": "text", "text": text}]})
        except Exception as e:
            return ok({"content": [{"type": "text", "text": f"出错：{e}"}], "isError": True})
    elif method in ("notifications/initialized", "notifications/cancelled"):
        return None
    else:
        return err(-32601, f"不支持: {method}")


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            resp = handle(json.loads(line))
            if resp is not None:
                print(json.dumps(resp, ensure_ascii=False), flush=True)
        except json.JSONDecodeError as e:
            print(json.dumps({
                "jsonrpc": "2.0", "id": None,
                "error": {"code": -32700, "message": str(e)}
            }), flush=True)


if __name__ == "__main__":
    main()
