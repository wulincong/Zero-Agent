"""常驻 bash 会话：Agent 的"手"（阶段 5：原生异步实现）。

设计要点：
1. stdout 与 stderr 分离：命令输出走 stdout，哨兵走 stderr（fd 2）。
2. 原生 asyncio 子进程（`create_subprocess_exec`）+ 非阻塞读取，
   兼顾小输出即时返回与无换行的长输出；不再依赖线程池。
3. 命令原样在当前 shell 执行，保证 cd / export 等状态保持生效。
4. 超时后返回已捕获输出并重启会话，避免挂起命令污染后续命令。

阶段 5 变更（相对同步版）
------------------------
- `subprocess.Popen` + `select` 轮询  ->  `asyncio.create_subprocess_exec`
  + `asyncio.wait_for(reader.read(...))` 轮询。
- `run()` 由同步方法改为 `async def arun()`；中断检测仍用 `ctrl.consume()`
  （阶段 3 的一次性语义），命中后向子进程发送 SIGINT。
- 确认回调由同步改为**异步**（`await confirm_callback(...)`）：异步化后
  确认流程运行在事件循环线程中，可直接 await 异步输入，无需线程桥接。
- 不再需要 `asyncio.to_thread` 桥接，`tools/builtin.py` 直接 `await bash.arun(...)`。
"""

import asyncio
import signal
import time
import uuid

from core import config as _config
from runtime.interrupt import get_interrupt
from security import Decision, check_command

# 单条命令的默认超时（秒），由 config.toml 的 [shell].timeout 控制
DEFAULT_TIMEOUT = _config.get_int("shell.timeout", 180)

# 中断标记（阶段 3）：命令被 Esc 中断时，返回值以此前缀开头。
# 内核（core/assistant.py）据此判定"工具被中断"，从而回滚本轮对话，
# 而不是把"[已中断]"这样的残缺结果写入历史（否则模型下一轮会看到它）。
INTERRUPT_MARKER = "__AGENT_INTERRUPTED__\n"

# 读取轮询周期（秒）：单次 read 等待上限，用于周期性检查中断/超时。
_POLL_INTERVAL = 0.05


# 退出码行前缀：脚本在 stderr 写入 `__EXIT:<code>__`，需从输出中剔除。
_EXIT_PREFIX = "__EXIT:"


def _strip_exit_marker(text: str) -> str:
    """从 stderr 文本中剔除退出码行（`__EXIT:<code>__`）。

    脚本形如 `cmd\necho __EXIT:$?__ >&2\necho <sentinel> >&2`，
    因此 stderr 中哨兵之前会残留一行退出码。该行属于内部协议，
    不应出现在返回给模型/用户的输出里。

    Args:
        text: 已按哨兵切分后的 stderr 文本。

    Returns:
        剔除退出码行后的文本（保留其余 stderr 内容）。
    """
    lines = [ln for ln in text.splitlines() if not ln.strip().startswith(_EXIT_PREFIX)]
    return "\n".join(lines)


class PersistentBash:
    """常驻 bash 会话（原生异步）。"""

    def __init__(self, confirm_callback=None):
        # confirm_callback(cmd, reason) -> Awaitable[bool]，返回 True 表示用户同意执行。
        # 为 None 时，CONFIRM 档一律拒绝（非交互/无人值守场景的安全默认）。
        # 阶段 5 起为异步回调（在事件循环线程中被 await）。
        self.confirm_callback = confirm_callback
        self.proc = None  # 延迟到首次使用时创建（需在事件循环内）

    # ------------------------------------------------------------------
    # 子进程生命周期
    # ------------------------------------------------------------------
    async def _ensure_proc(self):
        """确保 bash 子进程存在（首次调用时在事件循环内创建）。"""
        if self.proc is not None and self.proc.returncode is None:
            return
        self.proc = await asyncio.create_subprocess_exec(
            "/bin/bash",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

    async def _restart(self):
        """重启 bash 会话，清理挂起命令造成的状态污染。"""
        await self._kill_proc()
        self.proc = None
        await self._ensure_proc()

    async def _kill_proc(self):
        """终止当前子进程并关闭其管道（若存在）。

        显式关闭 stdin/stdout/stderr 管道，避免子进程 transport 在事件循环
        关闭后才被 GC 析构，从而抛出 "Event loop is closed" 警告。
        """
        proc = self.proc
        if proc is None:
            return
        try:
            if proc.returncode is None:
                proc.kill()
            await asyncio.wait_for(proc.wait(), timeout=5)
        except Exception:
            pass
        # 关闭管道与 transport，释放资源（防止事件循环关闭后的析构告警）
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            try:
                if stream is not None and not stream.is_closing():
                    stream.close()
            except Exception:
                pass
        try:
            transport = getattr(proc, "_transport", None)
            if transport is not None:
                transport.close()
        except Exception:
            pass
        # 让事件循环处理 transport 的关闭回调，避免其延迟到循环关闭后执行
        try:
            await asyncio.sleep(0)
        except Exception:
            pass

    async def close(self):
        """关闭会话（供 REPL 退出时调用）。"""
        await self._kill_proc()
        self.proc = None

    # ------------------------------------------------------------------
    # 命令执行
    # ------------------------------------------------------------------
    async def arun(self, cmd: str, timeout: float = DEFAULT_TIMEOUT,
                   interrupt=None) -> str:
        """在常驻 shell 中异步执行一条命令，返回其输出。

        Args:
            cmd: 待执行的命令原文（原样在当前 shell 执行，保持 cd/env 状态）。
            timeout: 单条命令超时秒数，超时后重启会话并返回已捕获输出。
            interrupt: 中断控制器；None 时使用全局单例。

        Returns:
            命令输出字符串。被中断时以 INTERRUPT_MARKER 开头。
        """
        # ---- 第一层：命令准入 ----
        verdict = check_command(cmd)
        if verdict.decision is Decision.DENY:
            return f"⛔ 命令被安全策略拦截：{verdict.reason}\n（命令未执行）"
        if verdict.decision is Decision.CONFIRM:
            if self.confirm_callback is None:
                return (
                    f"⛔ 命令需要用户确认，但当前无交互通道：{verdict.reason}\n"
                    f"（命令未执行）"
                )
            # 阶段 5：确认回调为异步（运行在事件循环线程中）
            approved = await self.confirm_callback(cmd, verdict.reason)
            if not approved:
                return f"⛔ 用户拒绝了该命令：{verdict.reason}\n（命令未执行）"

        await self._ensure_proc()
        if self.proc.returncode is not None:
            return "Error: bash 进程已退出，无法执行命令。"

        sentinel = f"__END_{uuid.uuid4().hex[:8]}__"
        # 哨兵与退出码写到 stderr，避免被命令的 stdout 输出或 stdin 读取干扰。
        # 命令原样在当前 shell 执行，保证 cd / export 等状态保持生效。
        script = f"{cmd}\necho __EXIT:$?__ >&2\necho {sentinel} >&2\n"
        try:
            self.proc.stdin.write(script.encode("utf-8"))
            await self.proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, ValueError) as e:
            return f"Error: 无法写入 bash 进程: {e}"

        ctrl = interrupt if interrupt is not None else get_interrupt()
        out_buf = b""
        err_buf = b""
        sentinel_b = sentinel.encode("utf-8")
        timed_out = False
        interrupted = False
        deadline = time.monotonic() + timeout

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            # 用户按 Esc：向 bash 子进程发送 SIGINT，打断当前前台命令。
            # 阶段 3 起用 consume() 消费中断标志：读取即清除，
            # 避免标志残留到主循环导致重复 rollback（见 REFACTOR_PLAN 阶段 3）。
            if ctrl.consume():
                interrupted = True
                self._send_sigint()
                # 给子进程一点时间响应，再收集残留输出
                await asyncio.sleep(0.1)
                out_buf, err_buf = await self._drain_available(out_buf, err_buf)
                break

            # 非阻塞读取：单次 read 最多等待 _POLL_INTERVAL，用于轮询中断/超时。
            out_buf, err_buf = await self._read_once(
                out_buf, err_buf, min(_POLL_INTERVAL, remaining)
            )
            if sentinel_b in err_buf:
                break

        out = out_buf.decode("utf-8", errors="replace").strip()
        # stderr 中除哨兵外还夹带退出码行（__EXIT:N__），需一并剔除，
        # 否则会污染命令输出（异步一次性读取时尤其明显）。
        err_raw = err_buf.split(sentinel_b)[0].decode("utf-8", errors="replace")
        err = _strip_exit_marker(err_raw).strip()
        if err:
            out = (out + "\n" + err).strip() if out else err
        if interrupted:
            # 中断后命令可能仍在运行，为保持会话干净，重启 bash。
            await self._restart()
            out += "\n[已中断] 用户按 Esc 中止了该命令，shell 会话已重置。"
            # 结构化标记（阶段 3）：内核据此识别"工具被中断"，
            # 从而回滚本轮而非把中断结果写入历史（解决竞态 C）。
            return INTERRUPT_MARKER + out
        if timed_out:
            # 挂起的命令仍在占用 stdin/stdout，会污染后续命令。
            # 直接重启 bash 会话，保证后续命令干净可用（代价是丢失 cd/env 状态）。
            await self._restart()
            out += (
                f"\n[警告] 命令在 {timeout}s 内未返回，可能仍在运行或等待输入"
                f"（如交互式命令）。已返回当前已捕获的输出，并已重置 shell 会话。"
            )
        return out

    # ------------------------------------------------------------------
    # 内部读取辅助
    # ------------------------------------------------------------------
    async def _read_once(self, out_buf: bytes, err_buf: bytes, wait: float):
        """非阻塞地各读一次 stdout / stderr，返回累积后的缓冲。

        单次读取最多等待 `wait` 秒；无数据则原样返回。
        """
        out_buf = await self._read_stream(self.proc.stdout, out_buf, wait)
        # stderr 用极短等待，避免拖慢主循环（哨兵通常紧随命令输出到达）
        err_buf = await self._read_stream(self.proc.stderr, err_buf, 0.001)
        return out_buf, err_buf

    @staticmethod
    async def _read_stream(reader, buf: bytes, wait: float) -> bytes:
        """从 StreamReader 读取一次可用数据（最多等待 wait 秒）。"""
        if reader is None:
            return buf
        try:
            chunk = await asyncio.wait_for(reader.read(65536), timeout=wait)
        except asyncio.TimeoutError:
            return buf
        except (ConnectionResetError, ValueError):
            return buf
        if chunk:
            return buf + chunk
        return buf

    async def _drain_available(self, out_buf: bytes, err_buf: bytes):
        """中断后非阻塞地收集当前所有可用输出。"""
        for _ in range(5):
            before = (len(out_buf), len(err_buf))
            out_buf, err_buf = await self._read_once(out_buf, err_buf, 0.02)
            if (len(out_buf), len(err_buf)) == before:
                break
        return out_buf, err_buf

    def _send_sigint(self) -> None:
        """向 bash 子进程发送 SIGINT，打断其前台命令。"""
        try:
            if self.proc is not None and self.proc.returncode is None:
                self.proc.send_signal(signal.SIGINT)
        except Exception:
            pass
