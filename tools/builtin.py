"""内置工具：run_bash / install_skill / reload_self。

每个工具以工厂函数形式提供，接收 assistant 实例（或所需依赖）后返回
一个被 @tool 装饰的可调用对象，便于主类在初始化与热重载时统一注册。
"""

import os

from langchain_core.tools import tool


def make_bash_tool(bash, on_output=None):
    """构造 run_bash 工具：在持久终端中执行命令。

    Args:
        bash: 持久终端实例。
        on_output: 可选回调 on_output(command, output)，用于把命令回显与输出
            交给上层（CLI）决定如何呈现（如折叠）。为 None 时回退为直接打印。
    """

    @tool
    def run_bash(command: str) -> str:
        """在持久终端中执行 Bash 命令。环境变量和目录切换全生命周期保持生效。"""
        from runtime.interrupt import get_interrupt

        out = bash.run(command, interrupt=get_interrupt())
        if on_output is not None:
            on_output(command, out)
        else:
            print(f"\n💻 [Shell]: {command}")
            print(f"📄 [Output]:\n{out}")
        return out

    return run_bash


def make_install_skill_tool(skills_dir, load_skill_file):
    """构造 install_skill 工具：写入并热加载一个新技能。

    Args:
        skills_dir: 技能库目录。
        load_skill_file: 回调 (skill_name, file_path) -> None，负责加载技能。
    """

    @tool
    def install_skill(skill_name: str, python_code: str) -> str:
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

    return install_skill


def make_reload_tool(assistant):
    """构造 reload_self 工具：登记一次热重载请求。"""

    @tool
    def reload_self() -> str:
        """热重载自身代码（core/ 与 security/），保留对话上下文与 shell 会话。
        当你修改了自身代码后，调用本工具使改动生效。
        注意：重载会在本轮对话结束后执行，本工具只负责登记请求。"""
        assistant._pending_reload = True
        return (
            "✅ 已登记热重载请求，将在本轮对话结束后自动执行。"
            "对话上下文与 shell 会话会保留。"
        )

    return reload_self
