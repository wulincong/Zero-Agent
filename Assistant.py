import glob
import importlib.util
import os
import select
import subprocess
import time
import sys
import uuid
from dotenv import load_dotenv
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI

from security import check_command, Decision

# 从当前目录的 .env 文件载入环境变量（不覆盖已存在的系统环境变量）
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

SKILLS_DIR = os.path.abspath("./my_skills")
os.makedirs(SKILLS_DIR, exist_ok=True)
if SKILLS_DIR not in sys.path:
    sys.path.append(SKILLS_DIR)

# 单条命令的默认超时（秒）
DEFAULT_TIMEOUT = 60


class PersistentBash:
    """常驻 bash 会话。

    设计要点：
    1. stdout 与 stderr 分离：命令输出走 stdout，哨兵走 stderr（fd 2）。
       这样即使命令读取 stdin（如 cat）或输出海量内容，哨兵通道也不受影响。
    2. 非阻塞 fd + select 轮询读取，兼顾小输出即时返回与无换行的长输出。
    3. 命令原样在当前 shell 执行，保证 cd / export 等状态保持生效。
    4. 超时后返回已捕获输出并重启会话，避免挂起命令污染后续命令。
    """

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

    def run(self, cmd: str, timeout: float = DEFAULT_TIMEOUT) -> str:
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
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
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
        if timed_out:
            # 挂起的命令仍在占用 stdin/stdout，会污染后续命令。
            # 直接重启 bash 会话，保证后续命令干净可用（代价是丢失 cd/env 状态）。
            self._restart()
            out += (
                f"\n[警告] 命令在 {timeout}s 内未返回，可能仍在运行或等待输入"
                f"（如交互式命令）。已返回当前已捕获的输出，并已重置 shell 会话。"
            )
        return out

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


class InteractiveAssistant:
    def __init__(self, api_key):
        self.api_key = api_key
        self.base_url = "https://api.deepseek.com"
        self.bash = PersistentBash(confirm_callback=self._confirm_command)
        self.tools_registry = {}
        # 常驻对话历史
        self.messages = []

        # 延迟重载标志：工具只置位，真正的重载在 chat() 返回后由主循环执行，
        # 避免在调用栈未清空时替换 self.__dict__ 导致行为不一致。
        self._pending_reload = False

        self.register_tool(self._create_bash_tool())
        self.register_tool(self._create_install_skill_tool())
        self.register_tool(self._create_reload_tool())

        self._bootstrap_existing_skills()  # 安装技能库

    @staticmethod
    def _confirm_command(cmd: str, reason: str) -> bool:
        """终端交互确认：危险命令执行前询问用户。"""
        print("\n" + "=" * 60)
        print(f"⚠️  需要确认：{reason}")
        print(f"    命令: {cmd}")
        print("=" * 60)
        try:
            ans = input("是否执行？[y/N] > ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return False
        return ans in ("y", "yes")

    def register_tool(self, tool_func):
        t = tool(tool_func) if not hasattr(tool_func, "name") else tool_func
        self.tools_registry[t.name] = t

    def _bootstrap_existing_skills(self):
        """扫描本地 skills 目录并自动热加载（单个文件出错不影响整体启动）"""
        skill_files = glob.glob(os.path.join(SKILLS_DIR, "*.py"))
        for fpath in skill_files:
            skill_name = os.path.splitext(os.path.basename(fpath))[0]
            try:
                self._load_skill_file(skill_name, fpath)
            except Exception as e:
                print(f"⚠️ 技能 [{skill_name}] 加载失败，已跳过: {e}")

    def _load_skill_file(self, skill_name: str, file_path: str):
        spec = importlib.util.spec_from_file_location(skill_name, file_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        func = getattr(mod, skill_name, None)
        if func:
            self.register_tool(tool(func))

    def _create_bash_tool(self):
        @tool
        def run_bash(command: str) -> str:
            """在持久终端中执行 Bash 命令。环境变量和目录切换全生命周期保持生效。"""
            print(f"\n💻 [Shell]: {command}")
            out = self.bash.run(command)
            print(f"📄 [Output]:\n{out}")
            return out
        return run_bash

    def _create_install_skill_tool(self):
        @tool
        def install_skill(skill_name: str, python_code: str) -> str:
            """编写并持久化安装一个通用 Python 函数为新技能，下次启动依然有效。必须带参数类型标注和 docstring。"""
            print(f"\n⚙️ [安装技能中...]: {skill_name}")
            file_path = os.path.join(SKILLS_DIR, f"{skill_name}.py")
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(python_code)
            try:
                self._load_skill_file(skill_name, file_path)
            except Exception as e:
                return f"❌ 技能 [{skill_name}] 已写入文件，但加载失败: {e}"
            return f"✅ 技能 [{skill_name}] 安装成功！后续对话可直接调用。"
        return install_skill

    def _create_reload_tool(self):
        @tool
        def reload_self() -> str:
            """热重载自身代码（Assistant.py 与 security.py），保留对话上下文与 shell 会话。
            当你修改了 Assistant.py 或 security.py 后，调用本工具使改动生效。
            注意：重载会在本轮对话结束后执行，本工具只负责登记请求。"""
            self._pending_reload = True
            return (
                "✅ 已登记热重载请求，将在本轮对话结束后自动执行。"
                "对话上下文与 shell 会话会保留。"
            )
        return reload_self

    @property
    def model(self):
        return ChatOpenAI(
            model="deepseek-chat",
            api_key=self.api_key,
            base_url=self.base_url,
            temperature=0
        ).bind_tools(list(self.tools_registry.values()))

    def reload_code(self) -> str:
        """热重载：重新加载 Assistant 模块与 security 模块，替换代码逻辑，
        但保留对话上下文（messages）与 shell 会话（bash 进程/cwd/env）。

        原理：状态迁移式重载。
        1. reload 模块，拿到新的类定义；
        2. 用旧状态（messages / bash / api_key）构造新实例；
        3. 把新实例的 __dict__ 覆盖到 self 上，self 引用不变。
        """
        import importlib

        # 记录需保留的状态
        preserved = {
            "messages": self.messages,
            "bash": self.bash,
            "api_key": self.api_key,
        }

        try:
            # 重载 security（策略可能被改过）
            if "security" in sys.modules:
                importlib.reload(sys.modules["security"])
            # 重载本模块
            mod = importlib.reload(sys.modules[__name__])
            new_cls = mod.InteractiveAssistant
        except Exception as e:
            return f"❌ 热重载失败（代码未替换，上下文完好）: {type(e).__name__}: {e}"

        try:
            # 用旧状态构造新实例（不新建 bash，避免丢 shell 状态）
            new_obj = new_cls.__new__(new_cls)
            new_obj.api_key = preserved["api_key"]
            new_obj.base_url = getattr(self, "base_url", "https://api.deepseek.com")
            new_obj.bash = preserved["bash"]
            new_obj.tools_registry = {}
            new_obj.messages = preserved["messages"]
            # 重新注册工具（使用新代码里的工具定义）
            new_obj._pending_reload = False
            new_obj.register_tool(new_obj._create_bash_tool())
            new_obj.register_tool(new_obj._create_install_skill_tool())
            new_obj.register_tool(new_obj._create_reload_tool())
            new_obj._bootstrap_existing_skills()
        except Exception as e:
            return f"❌ 热重载失败（新代码初始化出错，上下文完好）: {type(e).__name__}: {e}"

        # 原地替换：self 引用不变，但方法/属性全部换成新的
        self.__class__ = new_cls
        self.__dict__.update(new_obj.__dict__)
        return (
            f"✅ 热重载成功。上下文保留 {len(self.messages)} 条消息，"
            f"shell 会话未重启，已激活技能: {list(self.tools_registry.keys())}"
        )

    def chat(self, user_input: str) -> str:
        """多轮对话单步驱动器"""
        self.messages.append(HumanMessage(content=user_input))

        while True:
            try:
                ai_msg = self.model.invoke(self.messages)
            except Exception as e:
                return f"❌ 模型调用失败: {e}"

            self.messages.append(ai_msg)

            if not ai_msg.tool_calls:
                return ai_msg.content

            for call in ai_msg.tool_calls:
                fn_name = call["name"]
                fn_args = call["args"]

                target_tool = self.tools_registry.get(fn_name)
                if not target_tool:
                    res = f"Error: 未找到工具 {fn_name}"
                else:
                    if fn_name not in ["run_bash", "install_skill"]:
                        print(f"\n🔥 [触发已安装技能]: {fn_name}({fn_args})")
                    try:
                        res = target_tool.invoke(fn_args)
                    except Exception as e:
                        # 工具异常不中断对话，回填给模型让它自行纠错
                        res = f"Error: 工具 {fn_name} 执行失败: {type(e).__name__}: {e}"
                        print(f"⚠️ [工具异常]: {res}")

                self.messages.append(ToolMessage(
                    content=str(res),
                    tool_call_id=call["id"]
                ))


if __name__ == "__main__":
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        sys.exit(
            "❌ 未找到 DEEPSEEK_API_KEY。\n"
            "   请在当前目录创建 .env 文件并写入：DEEPSEEK_API_KEY=sk-xxx\n"
            "   或直接设置环境变量：export DEEPSEEK_API_KEY=sk-xxx"
        )

    assistant = InteractiveAssistant(api_key=api_key)
    print("=" * 60)
    print("🚀 个人专属智能助手已启动！")
    print("   指令: /reload 热重载代码(保留上下文) | /help 帮助 | exit/q 退出")
    print(f"📦 已激活技能库: {list(assistant.tools_registry.keys())}")
    print("=" * 60)

    try:
        while True:
            user_prompt = input("\n👤 You > ").strip()
            if not user_prompt:
                continue
            if user_prompt.lower() in ["exit", "quit", "q"]:
                print("👋 再见！环境与技能已保存。")
                break
            if user_prompt.lower() in ["/reload", "/r"]:
                print("\n" + assistant.reload_code())
                continue
            if user_prompt.lower() in ["/help", "/h"]:
                print("\n📖 可用指令：")
                print("   /reload  重新加载 Assistant.py 与 security.py（保留对话上下文与 shell 会话）")
                print("   /help    显示本帮助")
                print("   exit/q   退出")
                continue
            response = assistant.chat(user_prompt)
            print(f"\n🤖 Agent > {response}")

            if getattr(assistant, "_pending_reload", False):
                print("\n" + assistant.reload_code())
    finally:
        assistant.bash.close()
