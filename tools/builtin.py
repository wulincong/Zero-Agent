"""内置工具：run_bash / install_skill / reload_self。

每个工具以工厂函数形式提供，接收 assistant 实例（或所需依赖）后返回
一个被 @tool 装饰的可调用对象，便于主类在初始化与热重载时统一注册。

并发元数据（阶段 2 引入，为阶段 4 铺路）
----------------------------------------
每个工具通过 `_concurrency` 属性标注是否可并发执行：
  - run_bash / install_skill / reload_self -> 不可并发
    （分别共享 shell 会话、改技能注册表、改自身状态）
  - 技能库纯计算函数 -> 默认可并发
阶段 4 的并发执行器会读取该属性决定调度策略。
"""

import asyncio
import os

from langchain_core.tools import tool

# 并发元数据键名（阶段 4 读取）
CONCURRENCY_ATTR = "_concurrency"


def _mark_concurrency(tool_obj, concurrent: bool):
    """给工具对象打上"是否可并发"标记，供阶段 4 的调度器读取。"""
    try:
        setattr(tool_obj, CONCURRENCY_ATTR, concurrent)
    except Exception:
        pass
    return tool_obj


def make_bash_tool(bash, on_output=None):
    """构造 run_bash 工具：在持久终端中执行命令。

    阶段 2 起本工具为 async：内部用 `asyncio.to_thread` 把阻塞的
    `bash.run(...)` 桥接到线程池，避免阻塞事件循环。
    bash 本身仍是同步实现（唯一保留同步中断语义的模块，见 REFACTOR_PLAN 决策 1）。

    Args:
        bash: 持久终端实例。
        on_output: 可选回调 on_output(command, output)，用于把命令回显与输出
            交给上层（CLI）决定如何呈现（如折叠）。为 None 时回退为直接打印。
            注意：本回调在工作线程中被调用，实现需线程安全。
    """

    @tool
    async def run_bash(command: str) -> str:
        """在持久终端中执行 Bash 命令。环境变量和目录切换全生命周期保持生效。"""
        from runtime.interrupt import get_interrupt

        # bash.run 是阻塞调用（select 轮询 + 可能的 SIGINT），放到线程池执行，
        # 使事件循环保持可响应（Esc 中断仍由 bash 内部的 ctrl.is_set() 处理）。
        out = await asyncio.to_thread(bash.run, command, interrupt=get_interrupt())
        if on_output is not None:
            on_output(command, out)
        else:
            print(f"\n💻 [Shell]: {command}")
            print(f"📄 [Output]:\n{out}")
        return out

    return _mark_concurrency(run_bash, concurrent=False)


def make_install_skill_tool(skills_dir, load_skill_file):
    """构造 install_skill 工具：写入并热加载一个新技能。

    Args:
        skills_dir: 技能库目录。
        load_skill_file: 回调 (skill_name, file_path) -> None，负责加载技能。
    """

    @tool
    async def install_skill(skill_name: str, python_code: str) -> str:
        """编写并持久化安装一个通用 Python 函数为新技能，下次启动依然有效。必须带参数类型标注和 docstring。"""
        print(f"\n⚙️ [安装技能中...]: {skill_name}")
        file_path = os.path.join(skills_dir, f"{skill_name}.py")
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(python_code)
        try:
            load_skill_file(skill_name, file_path)
        except Exception as e:
            return f"❌ 技能 [{skill_name}] 已写入文件，但加载失败: {e}"
        return f"✅ 技能 [{skill_name}] 安装成功！后续对话可直接调用。"

    return _mark_concurrency(install_skill, concurrent=False)


def make_reload_tool(assistant):
    """构造 reload_self 工具：登记一次热重载请求。"""

    @tool
    async def reload_self() -> str:
        """热重载自身代码（core/ 与 security/），保留对话上下文与 shell 会话。
        当你修改了自身代码后，调用本工具使改动生效。
        注意：重载会在本轮对话结束后执行，本工具只负责登记请求。"""
        assistant._pending_reload = True
        return (
            "✅ 已登记热重载请求，将在本轮对话结束后自动执行。"
            "对话上下文与 shell 会话会保留。"
        )

    return _mark_concurrency(reload_self, concurrent=False)
