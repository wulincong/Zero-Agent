"""中断控制：让用户在 Agent 运行过程中按 Esc 打断，回到对话。

设计：
- InterruptController 维护一个线程安全的中断标志；
- 后台监听线程在 raw 模式下读取 stdin，检测到 Esc(\\x1b) 即置位；
- 模型调用（流式）与 bash 执行（select 循环）周期性检查该标志，
  从而在毫秒级内响应中断。

注意：监听线程仅在 TTY 下启用；非 TTY 环境自动退化为"不可中断"。
"""

import os
import select
import sys
import threading
import time

try:
    import termios
    import tty

    _HAS_TERMIOS = True
except Exception:  # pragma: no cover - 非 POSIX 环境
    _HAS_TERMIOS = False


class InterruptController:
    """线程安全的中断标志 + 键盘监听。"""

    def __init__(self):
        self._flag = threading.Event()
        self._stop = threading.Event()
        self._thread = None
        self._fd = None
        self._old_term = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # 状态查询 / 置位
    # ------------------------------------------------------------------
    def is_set(self) -> bool:
        return self._flag.is_set()

    def set(self) -> None:
        self._flag.set()

    def clear(self) -> None:
        self._flag.clear()

    # ------------------------------------------------------------------
    # 监听生命周期
    # ------------------------------------------------------------------
    def start(self) -> None:
        """启动后台键盘监听（仅 TTY 下有效）。"""
        if not _HAS_TERMIOS:
            return
        if not sys.stdin.isatty():
            return
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._flag.clear()
            self._thread = threading.Thread(
                target=self._listen, name="esc-listener", daemon=True
            )
            self._thread.start()

    def stop(self) -> None:
        """停止监听并恢复终端设置。"""
        with self._lock:
            self._stop.set()
            thread = self._thread
            self._thread = None
        if thread is not None:
            thread.join(timeout=1.0)
        self._restore_term()

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------
    def _listen(self) -> None:
        fd = sys.stdin.fileno()
        self._fd = fd
        try:
            self._old_term = termios.tcgetattr(fd)
            # cbreak：关闭行缓冲与回显，但保留信号（Ctrl+C 仍可中断）
            tty.setcbreak(fd)
        except Exception:
            self._old_term = None
            return

        try:
            while not self._stop.is_set():
                try:
                    r, _, _ = select.select([fd], [], [], 0.05)
                except (OSError, ValueError):
                    break
                if fd not in r:
                    continue
                try:
                    data = os.read(fd, 32)
                except (BlockingIOError, OSError):
                    continue
                if not data:
                    break
                # 检测 Esc：单独一个 \x1b 视为中断请求
                if b"\x1b" in data:
                    self._flag.set()
        finally:
            self._restore_term()

    def _restore_term(self) -> None:
        if self._old_term is not None and self._fd is not None:
            try:
                termios.tcsetattr(self._fd, termios.TCSADRAIN, self._old_term)
            except Exception:
                pass
            self._old_term = None


# 全局单例：供各层共享同一中断状态
_GLOBAL = InterruptController()


def get_interrupt() -> InterruptController:
    """获取全局中断控制器。"""
    return _GLOBAL
