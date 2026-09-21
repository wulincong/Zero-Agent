"""内核：InteractiveAssistant 主类。

职责：
- 装配各层（runtime / tools / memory / models / security）；
- 驱动多轮对话（模型调用 -> 工具执行 -> 结果回填）；
- 提供热重载能力（原地刷新代码，保留上下文与 shell 会话）。
"""

import glob
import importlib.util
import os
import sys

from langchain_core.messages import SystemMessage

from core.prompts import SYSTEM_PROMPT
from memory import ConversationMemory
from models import DEFAULT_BASE_URL, build_model
from runtime import PersistentBash
from tools import make_bash_tool, make_install_skill_tool, make_reload_tool

# 技能库目录（相对项目根目录）
SKILLS_DIR = os.path.abspath("./skills")
os.makedirs(SKILLS_DIR, exist_ok=True)
if SKILLS_DIR not in sys.path:
    sys.path.append(SKILLS_DIR)

# 热重载时需要原地刷新的模块（顺序：被依赖者在前）
RELOADABLE_MODULES = [
    "security.policy",
    "security",
    "core.prompts",
    "core.assistant",
]


class InteractiveAssistant:
    def __init__(self, api_key):
        self.api_key = api_key
        self.base_url = DEFAULT_BASE_URL
        self.bash = PersistentBash(confirm_callback=self._confirm_command)
        self.tools_registry = {}
        # 常驻对话历史：首条固定为系统提示词（自我认知），后续为对话消息
        self.memory = ConversationMemory(SYSTEM_PROMPT)

        # 延迟重载标志：工具只置位，真正的重载在 chat() 返回后由主循环执行，
        # 避免在调用栈未清空时替换 self.__dict__ 导致行为不一致。
        self._pending_reload = False

        self._register_builtin_tools()
        self._bootstrap_existing_skills()  # 安装技能库

    # ------------------------------------------------------------------
    # 兼容属性：messages 直接映射到 memory.messages
    # ------------------------------------------------------------------
    @property
    def messages(self):
        return self.memory.messages

    @messages.setter
    def messages(self, value):
        self.memory.messages = value

    # ------------------------------------------------------------------
    # 工具注册
    # ------------------------------------------------------------------
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
        t = tool_func if hasattr(tool_func, "name") else tool_func
        self.tools_registry[t.name] = t

    def _register_builtin_tools(self):
        self.register_tool(make_bash_tool(self.bash))
        self.register_tool(make_install_skill_tool(SKILLS_DIR, self._load_skill_file))
        self.register_tool(make_reload_tool(self))

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
            from langchain_core.tools import tool
            self.register_tool(tool(func))

    # ------------------------------------------------------------------
    # 模型
    # ------------------------------------------------------------------
    @property
    def model(self):
        return build_model(
            api_key=self.api_key,
            tools=self.tools_registry.values(),
            base_url=self.base_url,
        )

    # ------------------------------------------------------------------
    # 热重载
    # ------------------------------------------------------------------
    @staticmethod
    def _reload_module(module):
        """重载一个已加载的模块，兼容 `python xxx.py` 启动的场景。

        读取模块源码并在其自身命名空间中原地执行，刷新代码逻辑；
        执行时跳过 `if __name__ == "__main__":` 主循环块。
        模块对象身份不变，sys.modules 与外部引用仍指向同一对象。
        """
        file_path = getattr(module, "__file__", None)
        if not file_path:
            raise RuntimeError(f"模块 {module.__name__} 无 __file__，无法重载")

        with open(file_path, "r", encoding="utf-8") as f:
            source = f.read()
        code = compile(source, file_path, "exec")

        # 在模块自身命名空间中执行，原地刷新；临时改名以跳过 __main__ 块
        ns = module.__dict__
        original_name = ns.get("__name__")
        try:
            if original_name == "__main__":
                ns["__name__"] = "__reloaded_main__"
            exec(code, ns)
        finally:
            ns["__name__"] = original_name
        return module

    def reload_code(self) -> str:
        """热重载：重新加载内核与安全模块，替换代码逻辑，
        但保留对话上下文（messages）与 shell 会话（bash 进程/cwd/env）。
        在对本Agent进行功能更新时自动热重载。

        【能力边界与限制说明】
        ------------------------------------------------------------------------
        ✅ 适用场景（推荐使用热重载，秒级生效）：
          - 纯逻辑修复：修改方法体内部代码、调整 Prompt 模版、优化异常处理与日志。
          - 工具增删改：新增/移除 Tool、修改现有 Tool 的执行逻辑或入参描述。
          - 规则配置更新：更新 security/ 中的安全策略、命令过滤规则、黑白名单。
          - 新增普通方法：类中新增的方法随原型链立即生效。

        ❌ 不支持 / 存在风险（必须进行完整冷重启进程）：
          - 状态结构（Schema）变更：如重构了 `messages` 的数据结构。
          - 常驻资源与后台任务：若旧代码启动了独立的后台线程（Thread）、异步任务
            （asyncio.Task）或网络 Socket 监听，reload 不会自动清理旧资源。
          - 跨模块静态引用：若外部模块通过 `from xxx import InteractiveAssistant`
            持有类引用，外部依然会指向旧类指针，无法同步更新。
        ------------------------------------------------------------------------
        """
        # 记录需保留的状态
        preserved = {
            "memory": self.memory,
            "bash": self.bash,
            "api_key": self.api_key,
        }

        try:
            # 按依赖顺序原地重载各模块（策略/提示词可能被改过）
            for name in RELOADABLE_MODULES:
                if name in sys.modules:
                    self._reload_module(sys.modules[name])
            mod = sys.modules["core.assistant"]
            new_cls = mod.InteractiveAssistant
            new_prompt = sys.modules["core.prompts"].SYSTEM_PROMPT
        except Exception as e:
            return f"❌ 热重载失败（代码未替换，上下文完好）: {type(e).__name__}: {e}"

        try:
            # 用旧状态构造新实例（不新建 bash，避免丢 shell 状态）
            new_obj = new_cls.__new__(new_cls)
            new_obj.api_key = preserved["api_key"]
            new_obj.base_url = getattr(self, "base_url", DEFAULT_BASE_URL)
            new_obj.bash = preserved["bash"]
            new_obj.tools_registry = {}
            new_obj.memory = preserved["memory"]
            new_obj._pending_reload = False
            # 重新注册工具（使用新代码里的工具定义）
            new_obj._register_builtin_tools()
            new_obj._bootstrap_existing_skills()
        except Exception as e:
            return f"❌ 热重载失败（新代码初始化出错，上下文完好）: {type(e).__name__}: {e}"

        # 原地替换：self 引用不变，但方法/属性全部换成新的
        self.__class__ = new_cls
        self.__dict__.update(new_obj.__dict__)
        # 刷新系统提示词：若新代码改了 SYSTEM_PROMPT，立即替换历史首条 system 消息
        self.memory.ensure_system_prompt(new_prompt)
        return (
            f"✅ 热重载成功。上下文保留 {len(self.memory)} 条消息，"
            f"shell 会话未重启，已激活技能: {list(self.tools_registry.keys())}"
        )

    # ------------------------------------------------------------------
    # 对话驱动
    # ------------------------------------------------------------------
    def chat(self, user_input: str) -> str:
        """多轮对话单步驱动器"""
        # 兜底：确保系统提示词始终位于对话历史首位（热重载/异常后自愈）
        self.memory.ensure_system_prompt()

        from langchain_core.messages import ToolMessage

        self.memory.add_user(user_input)

        while True:
            try:
                ai_msg = self.model.invoke(self.memory.messages)
            except Exception as e:
                return f"❌ 模型调用失败: {e}"

            self.memory.add(ai_msg)

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

                self.memory.add(ToolMessage(
                    content=str(res),
                    tool_call_id=call["id"]
                ))
