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


# 判定"孤立 Esc"时的等待窗口（秒）。
# 终端转义序列（方向键等）的后续字节几乎立即到达，30ms 足以区分；
# 过大会让 Esc 响应变迟钝，过小可能误判。
ESC_SEQ_TIMEOUT = 0.03


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

    def consume(self) -> bool:
        """读取并**清除**中断标志（原子操作），返回此前是否处于中断态。

        阶段 3 引入。中断是"一次性事件"而非"持续状态"：
        同一个 Esc 只应被响应一次。若用 is_set() 判断后不清除，
        中断标志会残留到下一轮对话，导致：
          - bash 中断后回到主循环，for 循环开头再次 is_set() 为真，
            触发重复 rollback 与重复的"已中断"提示；
          - 下一轮 achat 开头虽会 clear()，但若中断发生在
            clear() 与 start() 之间，真实中断会被吞掉。

        用 consume() 取代"is_set() + 事后 clear()"的组合，
        保证一次中断只被消费一次，消除上述竞态。

        Returns:
            True 表示本次调用前标志已置位（即确实发生了一次中断）。
        """
        # threading.Event 无原子 test-and-clear，用锁保证读-清不可分割
        with self._lock:
            was_set = self._flag.is_set()
            if was_set:
                self._flag.clear()
            return was_set

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
                # 检测 Esc：仅"孤立的 \x1b"才算中断请求。
                # 方向键/Home/End/Alt+Enter 等都会以 \x1b 开头（转义序列），
                # 若一并视为 Esc 会造成误中断，故需区分。
                if self._is_lone_escape(fd, data):
                    self._flag.set()
        finally:
            self._restore_term()

    @staticmethod
    def _is_lone_escape(fd: int, data: bytes) -> bool:
        """判断本次读到的数据是否代表"用户按下了 Esc 键"。

        终端里 Esc 键本身只产生一个字节 \x1b；而方向键、Home/End、
        Alt+Enter 等按键产生的是以 \x1b 开头的多字节转义序列
        （如 \x1b[A、\x1b[H、\x1b\r）。

        判定策略：
        - 若 \x1b 之后还有字节，说明是转义序列，不是 Esc；
        - 若 \x1b 是最后一个字节，则短暂等待（ESC_SEQ_TIMEOUT）看是否
          还有后续字节：有则是转义序列，没有才是真正的 Esc。

        Args:
            fd: 正在监听的 stdin 文件描述符。
            data: 本次 os.read 读到的原始字节。

        Returns:
            True 表示用户按下了 Esc（应触发中断）。
        """
        idx = data.find(b"\x1b")
        if idx == -1:
            return False
        # \x1b 之后仍有字节 -> 转义序列（方向键等），不是 Esc
        if idx < len(data) - 1:
            return False
        # \x1b 位于末尾：等待极短时间，看是否还有后续字节
        try:
            r, _, _ = select.select([fd], [], [], ESC_SEQ_TIMEOUT)
        except (OSError, ValueError):
            return True
        if r:
            # 有后续字节 -> 是转义序列，丢弃并忽略
            try:
                os.read(fd, 32)
            except (BlockingIOError, OSError):
                pass
            return False
        # 无后续字节 -> 孤立的 Esc
        return True

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
