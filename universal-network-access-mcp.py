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
import time
import tempfile
import threading
import atexit
import signal


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
# 传输层:所有 stdout 写入都经过 send() + 全局锁序列化
# ============================================================
_write_lock = threading.Lock()


def send(obj, out=None):
    stream = out if out is not None else sys.stdout
    with _write_lock:
        stream.write(json.dumps(obj, ensure_ascii=False) + "\n")
        stream.flush()


def ok(rid, result):
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def err(rid, code, message):
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


def content(text, is_error=False):
    r = {"content": [{"type": "text", "text": text}]}
    if is_error:
        r["isError"] = True
    return r


def _clip(s, n=500):
    """截断异常串并剔除控制字符（保留 \\t \\n），避免回显噪声/敏感信息过长。"""
    s = str(s)
    s = "".join(ch for ch in s if ch >= " " or ch in "\t\n")
    return s[:n]


def _safe_timeout(v, default=30):
    """安全解析 timeout：无法转 int 或 ≤0 → default；超大按原样接受（调用方责任）。绝不抛。"""
    try:
        t = int(v)
    except (TypeError, ValueError):
        return default
    return t if t > 0 else default


def send_progress(token, progress, total, message, out=None):
    if token is None:
        return
    send({
        "jsonrpc": "2.0",
        "method": "notifications/progress",
        "params": {
            "progressToken": token,
            "progress": progress,
            "total": total,
            "message": message,
        },
    }, out=out)


# ============================================================
# 任务表:支持 background 轮询与取消
# ============================================================
_jobs = {}
_jobs_lock = threading.Lock()
_job_counter = 0
_finish_counter = 0                # 进入终态的单调序号，用于淘汰最旧终态 job
MAX_TERMINAL_JOBS = 100            # _jobs 中终态 job 的容量上界


def is_terminal(status):
    return status in ("done", "error", "timeout", "cancelled")


class Job:
    def __init__(self, job_id, request_id):
        self.id = job_id
        self.request_id = request_id
        self.status = "running"   # running | done | error | cancelled | timeout
        self.result_text = None
        self.cancel_event = threading.Event()
        self.start = time.monotonic()
        self.message = "正在启动…"
        self.finish_seq = None    # 进入终态时由 finish_job 分配


def new_job(request_id):
    global _job_counter
    with _jobs_lock:
        _job_counter += 1
        job = Job("job-%d" % _job_counter, request_id)
        _jobs[job.id] = job
        return job


def get_job(job_id):
    with _jobs_lock:
        return _jobs.get(job_id)


def find_running_by_request(request_id):
    with _jobs_lock:
        for j in _jobs.values():
            if j.request_id == request_id and j.status == "running":
                return j
    return None


def finish_job(job, status, text):
    """在 _jobs_lock 内原子完成：置终态 + 分配完成序号 + 容量淘汰最旧终态 job。
    所有终态转换必须经此函数（不变量 INV-1）；running job 绝不被淘汰。"""
    global _finish_counter
    with _jobs_lock:
        job.status = status
        job.result_text = text
        _finish_counter += 1
        job.finish_seq = _finish_counter
        # 只统计经 finish_job 赋过序号的终态 job（手工设 status 的测试 job finish_seq=None，不参与，防 None 排序）
        terminal = [j for j in _jobs.values()
                    if is_terminal(j.status) and j.finish_seq is not None]
        if len(terminal) > MAX_TERMINAL_JOBS:
            terminal.sort(key=lambda j: j.finish_seq)
            for old in terminal[:len(terminal) - MAX_TERMINAL_JOBS]:
                _jobs.pop(old.id, None)


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


PROGRESS_INTERVAL = 2.0   # 每隔多少秒发一次 progress
POLL_INTERVAL = 0.2       # 轮询子进程的间隔


class Cancelled(Exception):
    """子进程被客户端取消时抛出。"""


def _terminate(proc):
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


def run_subprocess(cmd, timeout, shell=False, on_tick=None,
                   cancel_event=None, decode="utf-8"):
    """用 Popen 执行命令,轮询期间回调 on_tick(elapsed)。
    返回 (returncode, stdout, stderr)。
    超时抛 subprocess.TimeoutExpired,取消抛 Cancelled。
    stdout/stderr 写入临时文件,避免 PIPE 缓冲写满导致死锁。
    """
    proc = None                       # 前置绑定，防 Popen 抛异常时 finally 引用 proc NameError
    out_f = tempfile.TemporaryFile()
    err_f = tempfile.TemporaryFile()
    try:
        proc = subprocess.Popen(cmd, stdout=out_f, stderr=err_f, shell=shell)
        _register_proc(proc)
        start = time.monotonic()
        last_tick = 0.0
        while proc.poll() is None:
            elapsed = time.monotonic() - start
            if cancel_event is not None and cancel_event.is_set():
                _terminate(proc)
                raise Cancelled()
            if elapsed > timeout:
                _terminate(proc)
                raise subprocess.TimeoutExpired(cmd, timeout)
            if on_tick is not None and (elapsed - last_tick) >= PROGRESS_INTERVAL:
                last_tick = elapsed
                on_tick(elapsed)
            time.sleep(POLL_INTERVAL)
        out_f.seek(0)
        err_f.seek(0)
        raw_out = out_f.read()
        raw_err = err_f.read()
    finally:
        if proc is not None:
            _unregister_proc(proc)
        out_f.close()
        err_f.close()
    return (
        proc.returncode,
        raw_out.decode(decode, errors="replace"),
        raw_err.decode(decode, errors="replace"),
    )


_pip_lock = threading.Lock()


# ============================================================
# 活跃子进程注册表：退出时尽力终止运行中的子进程（独立锁）
# ============================================================
_procs = set()
_procs_lock = threading.Lock()


def _register_proc(p):
    with _procs_lock:
        _procs.add(p)
    try:
        sys.stderr.write("[proc] pid=%s\n" % p.pid)
        sys.stderr.flush()
    except Exception:
        pass


def _unregister_proc(p):
    with _procs_lock:
        _procs.discard(p)   # 幂等


def _shutdown_procs():
    """遍历注册表逐个 terminate（超时则 kill）。锁内取快照、锁外终止；对任何 Popen 的异常一律吞。"""
    with _procs_lock:
        snapshot = list(_procs)
    for p in snapshot:
        try:
            p.terminate()
            try:
                p.wait(timeout=3)
            except subprocess.TimeoutExpired:
                p.kill()
        except Exception:
            pass


def make_on_tick(progress_token, total, job):
    """返回一个 on_tick;进度用 job.start 起算保证单调递增,
    message 取 job.message(各阶段可变:执行中 / 安装中)。"""
    def on_tick(_elapsed):
        send_progress(
            progress_token,
            round(time.monotonic() - job.start, 1),
            total,
            job.message,
        )
    return on_tick


def tool_run_python(args, on_tick, job):
    code = args.get("code", "")
    timeout = _safe_timeout(args.get("timeout", 30))
    if not code:
        return "请提供 code 参数"

    job.message = "正在执行 Python…"
    rc, out, err = run_subprocess(
        [PYTHON, "-c", code], timeout,
        on_tick=on_tick, cancel_event=job.cancel_event,
    )

    # 自动处理缺包：检测 ModuleNotFoundError 就 pip install 后重试（最多 3 次）
    for _ in range(3):
        if rc != 0 and "ModuleNotFoundError" in err:
            module = _extract_missing_module(err)
            if not module:
                break
            job.message = "正在安装 %s…" % module
            with _pip_lock:
                irc, _iout, ierr = run_subprocess(
                    [PYTHON, "-m", "pip", "install", module, "-q"], 120,
                    on_tick=on_tick, cancel_event=job.cancel_event,
                )
            if irc != 0:
                return "自动安装 %s 失败:\n%s" % (module, ierr)
            job.message = "正在执行 Python…"
            rc, out, err = run_subprocess(
                [PYTHON, "-c", code], timeout,
                on_tick=on_tick, cancel_event=job.cancel_event,
            )
        else:
            break

    result = "退出码:%d" % rc
    if out:
        result += "\n\n输出:\n%s" % out[:8000]
    if err:
        result += "\n\n错误:\n%s" % err[:3000]
    return result


def tool_run_shell(args, on_tick, job):
    command = args.get("command", "")
    shell_type = args.get("shell", "cmd")
    timeout = _safe_timeout(args.get("timeout", 30))
    if not command:
        return "请提供 command 参数"

    job.message = "正在执行命令…"
    if shell_type == "powershell":
        cmd = ["powershell", "-NoProfile", "-NonInteractive", "-Command", command]
        rc, out, err = run_subprocess(
            cmd, timeout, on_tick=on_tick,
            cancel_event=job.cancel_event, decode="utf-8")
    elif shell_type == "bash":
        cmd = ["bash", "-c", command]
        rc, out, err = run_subprocess(
            cmd, timeout, on_tick=on_tick,
            cancel_event=job.cancel_event, decode="utf-8")
    else:  # cmd 默认,Windows 输出按 gbk 解码
        rc, out, err = run_subprocess(
            command, timeout, shell=True, on_tick=on_tick,
            cancel_event=job.cancel_event, decode="gbk")

    result = "退出码:%d" % rc
    if out:
        result += "\n\nSTDOUT:\n%s" % out[:8000]
    if err:
        result += "\n\nSTDERR:\n%s" % err[:3000]
    return result


def tool_check_job(args):
    job_id = args.get("job_id", "")
    job = get_job(job_id)             # 非消费性：读取不删除
    if job:
        if job.status == "running":
            elapsed = int(time.monotonic() - job.start)
            return "运行中… 已耗时 %ds,当前:%s" % (elapsed, job.message)
        return "[%s]\n%s" % (job.status, job.result_text)
    # 未命中：区分"曾存在但被挤出" vs "从未存在"
    m = re.fullmatch(r"job-(\d+)", job_id or "")   # 解析失败即视为未找到
    if m and int(m.group(1)) <= _job_counter:
        return "结果已被更新的任务挤出（勿重跑，以最后一次结果为准）:%s" % job_id
    return "未找到 job:%s" % job_id


# ============================================================
# 工具定义（MCP Schema）
# ============================================================

TOOLS = [
    {
        "name": "run_python",
        "description": (
            "在用户本机执行任意 Python 代码,具有完整网络访问能力,可突破 Claude 沙箱限制。"
            "自动检测并安装缺少的第三方库(ModuleNotFoundError 时自动 pip install 并重试)。"
            "可 import 任何库:requests/pymysql/redis/paramiko/ftplib/smtplib/pymongo 等。"
            "适合:连接 MySQL/Redis/FTP/SSH/HTTP/SMTP 等任意网络服务,数据处理,文件操作。"
            "注意:timeout 只是服务端子进程的上限,并不会延长客户端(Claude)的等待时限;"
            "执行期间服务器会持续发送进度通知以避免客户端超时。"
            "对预计很久的任务,请传 background:true 立即拿到 job_id,再用 check_job 轮询结果。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "code":       {"type": "string",  "description": "完整 Python 代码,用 print() 输出结果"},
                "timeout":    {"type": "integer", "description": "服务端子进程超时秒数,默认 30", "default": 30},
                "background": {"type": "boolean", "description": "true 则后台执行,立即返回 job_id,用 check_job 轮询", "default": False},
            },
            "required": ["code"],
        },
    },
    {
        "name": "run_shell",
        "description": (
            "在用户本机执行系统命令,支持 cmd(默认)/ PowerShell / bash。"
            "用途:pip install 安装依赖、curl/ssh/ftp 命令、查看网络/进程/文件、管理服务等。"
            "注意:timeout 只是服务端子进程上限,不延长客户端等待;长任务请用 background:true + check_job。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "要执行的命令"},
                "shell":   {
                    "type": "string",
                    "description": "Shell 类型:cmd(默认)/ powershell / bash",
                    "enum": ["cmd", "powershell", "bash"],
                    "default": "cmd"
                },
                "timeout":    {"type": "integer", "description": "服务端子进程超时秒数,默认 30", "default": 30},
                "background": {"type": "boolean", "description": "true 则后台执行,立即返回 job_id,用 check_job 轮询", "default": False},
            },
            "required": ["command"],
        },
    },
    {
        "name": "check_job",
        "description": "查询 background 任务的状态与结果。运行中返回进度与已耗时;完成则返回退出码与输出。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "job_id": {"type": "string", "description": "run_python/run_shell 在 background 模式下返回的 job_id"},
            },
            "required": ["job_id"],
        },
    },
]


# ============================================================
# Worker:在独立线程里执行 run_python / run_shell
# ============================================================
def worker(job, name, arguments, request_id, progress_token, background):
    # responded 单标志：ack 与终态响应共用；每次 send 后即置位，finally 只兜底"漏发"。
    responded = False
    total = _safe_timeout(arguments.get("timeout", 30))   # 非法→30，绝不抛
    on_tick = make_on_tick(progress_token, total, job)

    if background:
        send(ok(request_id, content(
            "已在后台启动,job_id=%s。请用 check_job 轮询结果。" % job.id)))
        responded = True   # ack 即 background 的唯一响应

    try:
        if name == "run_python":
            text = tool_run_python(arguments, on_tick, job)
        else:  # run_shell
            text = tool_run_shell(arguments, on_tick, job)
        finish_job(job, "done", text)
        if not background and not responded:
            send(ok(request_id, content(text)))
            responded = True
    except subprocess.TimeoutExpired:
        finish_job(job, "timeout", "执行超时(%ds)" % total)
        if not background and not responded:
            send(ok(request_id, content(job.result_text, is_error=True)))
            responded = True
    except Cancelled:
        finish_job(job, "cancelled", "已取消")
        if not background and not responded:
            send(ok(request_id, content(job.result_text, is_error=True)))
            responded = True
    except Exception as e:  # 兜底,绝不让线程崩溃
        finish_job(job, "error", "执行失败:%s" % _clip(str(e)))
        if not background and not responded:
            send(ok(request_id, content(job.result_text, is_error=True)))
            responded = True
    finally:
        # 未预期路径（如 except Exception 抓不到的情形）：保证必达终态 + 非 bg 不漏响应
        if job.status == "running":
            finish_job(job, "error", "worker 异常退出")
        if not background and not responded:
            send(ok(request_id, content(job.result_text, is_error=True)))


# ============================================================
# MCP 协议（JSON-RPC over stdio）
# 调度:reader 线程只解析/路由,绝不阻塞在执行上
# ============================================================
def dispatch(req):
    if not isinstance(req, dict):
        send(err(None, -32600, "请求必须是 JSON 对象"))
        return
    method = req.get("method")
    rid = req.get("id")
    params = req.get("params", {})

    if method == "initialize":
        send(ok(rid, {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "universal-local-mcp", "version": "3.0.0"},
        }))
    elif method == "tools/list":
        send(ok(rid, {"tools": TOOLS}))
    elif method == "ping":
        send(ok(rid, {}))
    elif method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments", {})
        progress_token = (params.get("_meta") or {}).get("progressToken")
        if name == "check_job":
            send(ok(rid, content(tool_check_job(arguments))))
        elif name in ("run_python", "run_shell"):
            background = bool(arguments.get("background", False))
            job = new_job(rid)
            t = threading.Thread(
                target=worker,
                args=(job, name, arguments, rid, progress_token, background),
                daemon=True,
            )
            try:
                t.start()
            except Exception as e:
                # 线程启动失败：先置终态杜绝 running 残留，再回 -32603
                finish_job(job, "error", "线程启动失败: %s" % _clip(str(e)))
                send(err(rid, -32603, _clip("线程启动失败: %s" % e)))
        else:
            send(err(rid, -32601, "未知工具: %s" % name))
    elif method == "notifications/cancelled":
        job = find_running_by_request(params.get("requestId"))
        if job:
            job.cancel_event.set()
    elif method in ("notifications/initialized",):
        pass
    else:
        if rid is not None:
            send(err(rid, -32601, "不支持: %s" % method))


def _sig_handler(signum, frame):
    _shutdown_procs()
    os._exit(0)   # 固定 os._exit：跳过 finally/atexit，避免清理逻辑三重再入


def _install_signal_handlers():
    # signal 只能在主线程注册；SIGTERM 在 Windows 支持有限 → 仅 POSIX + 主线程安装
    if os.name == "posix" and threading.current_thread() is threading.main_thread():
        try:
            signal.signal(signal.SIGTERM, _sig_handler)
            signal.signal(signal.SIGINT, _sig_handler)
        except Exception:
            pass


def main():
    _install_signal_handlers()
    atexit.register(_shutdown_procs)   # 兜底：正常返回 / 未捕获异常
    try:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                req = json.loads(line)
            except json.JSONDecodeError as e:
                send(err(None, -32700, _clip(str(e))))
                continue
            try:
                dispatch(req)
            except Exception as e:
                # dispatch 内部任意异常都不得终止主循环
                rid = req.get("id") if isinstance(req, dict) else None
                if rid is not None:
                    send(err(rid, -32603, _clip(str(e))))
    finally:
        _shutdown_procs()   # stdin EOF / 主循环异常退出时清理运行中子进程


if __name__ == "__main__":
    main()
