"""常驻 bash 会话：Agent 的"手"。

设计要点：
1. stdout 与 stderr 分离：命令输出走 stdout，哨兵走 stderr（fd 2）。
2. 非阻塞 fd + select 轮询读取，兼顾小输出即时返回与无换行的长输出。
3. 命令原样在当前 shell 执行，保证 cd / export 等状态保持生效。
4. 超时后返回已捕获输出并重启会话，避免挂起命令污染后续命令。
"""

import os
import select
import signal
import subprocess
import time
import uuid

from core import config as _config
from runtime.interrupt import get_interrupt
from security import Decision, check_command

# 单条命令的默认超时（秒），由 config.toml 的 [shell].timeout 控制
DEFAULT_TIMEOUT = _config.get_int("shell.timeout", 180)


class PersistentBash:
    """常驻 bash 会话。"""

    def __init__(self, confirm_callback=None):
        # confirm_callback(cmd, reason) -> bool，返回 True 表示用户同意执行。
        # 为 None 时，CONFIRM 档一律拒绝（非交互/无人值守场景的安全默认）。
        self.confirm_callback = confirm_callback
        self.proc = subprocess.Popen(
            ["/bin/bash"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        # 用非阻塞 fd + select 读取，兼顾小输出即时返回与无换行长输出
        self._out_fd = self.proc.stdout.fileno()
        self._err_fd = self.proc.stderr.fileno()
        os.set_blocking(self._out_fd, False)
        os.set_blocking(self._err_fd, False)
        self._out_buf = ""
        self._err_buf = ""

    @staticmethod
    def _drain(fd, buf):
        """读取 fd 上所有可用数据，追加到缓冲并返回。"""
        try:
            data = os.read(fd, 65536)
        except (BlockingIOError, OSError):
            return buf
        if not data:
            return buf
        return buf + data.decode("utf-8", errors="replace")

    def run(self, cmd: str, timeout: float = DEFAULT_TIMEOUT, interrupt=None) -> str:
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
            if not self.confirm_callback(cmd, verdict.reason):
                return f"⛔ 用户拒绝了该命令：{verdict.reason}\n（命令未执行）"

        if self.proc.poll() is not None:
            return "Error: bash 进程已退出，无法执行命令。"

        sentinel = f"__END_{uuid.uuid4().hex[:8]}__"
        # 哨兵与退出码写到 stderr，避免被命令的 stdout 输出或 stdin 读取干扰。
        # 命令原样在当前 shell 执行，保证 cd / export 等状态保持生效。
        script = f"{cmd}\necho __EXIT:$?__ >&2\necho {sentinel} >&2\n"
        try:
            self.proc.stdin.write(script)
            self.proc.stdin.flush()
        except (BrokenPipeError, ValueError) as e:
            return f"Error: 无法写入 bash 进程: {e}"

        timed_out = False
        interrupted = False
        ctrl = interrupt if interrupt is not None else get_interrupt()
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            # 用户按 Esc：向 bash 子进程发送 SIGINT，打断当前前台命令
            if ctrl.is_set():
                interrupted = True
                self._send_sigint()
                # 给子进程一点时间响应，再收集残留输出
                time.sleep(0.1)
                self._drain_available()
                break
            r, _, _ = select.select([self._out_fd, self._err_fd], [], [], min(0.05, remaining))
            if self._out_fd in r:
                self._out_buf = self._drain(self._out_fd, self._out_buf)
            if self._err_fd in r:
                self._err_buf = self._drain(self._err_fd, self._err_buf)
            if sentinel in self._err_buf:
                break

        out = self._out_buf.strip()
        err = self._err_buf.split(sentinel)[0].strip()
        self._out_buf = ""
        self._err_buf = ""
        if err:
            out = (out + "\n" + err).strip() if out else err
        if interrupted:
            # 中断后命令可能仍在运行，为保持会话干净，重启 bash。
            self._restart()
            out += "\n[已中断] 用户按 Esc 中止了该命令，shell 会话已重置。"
            return out
        if timed_out:
            # 挂起的命令仍在占用 stdin/stdout，会污染后续命令。
            # 直接重启 bash 会话，保证后续命令干净可用（代价是丢失 cd/env 状态）。
            self._restart()
            out += (
                f"\n[警告] 命令在 {timeout}s 内未返回，可能仍在运行或等待输入"
                f"（如交互式命令）。已返回当前已捕获的输出，并已重置 shell 会话。"
            )
        return out

    def _send_sigint(self) -> None:
        """向 bash 子进程发送 SIGINT，打断其前台命令。"""
        try:
            self.proc.send_signal(signal.SIGINT)
        except Exception:
            pass

    def _drain_available(self) -> None:
        """非阻塞地收集当前所有可用输出。"""
        for _ in range(5):
            r, _, _ = select.select([self._out_fd, self._err_fd], [], [], 0.02)
            if not r:
                break
            if self._out_fd in r:
                self._out_buf = self._drain(self._out_fd, self._out_buf)
            if self._err_fd in r:
                self._err_buf = self._drain(self._err_fd, self._err_buf)

    def _restart(self):
        """重启 bash 会话，清理挂起命令造成的状态污染。"""
        try:
            self.proc.kill()
            self.proc.wait(timeout=5)
        except Exception:
            pass
        self.__init__(confirm_callback=self.confirm_callback)

    def close(self):
        try:
            self.proc.terminate()
            self.proc.wait(timeout=5)
        except Exception:
            try:
                self.proc.kill()
                self.proc.wait(timeout=5)
            except Exception:
                pass
