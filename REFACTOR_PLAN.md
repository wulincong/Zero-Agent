# 异步事件驱动重构 · 进度追踪

> **本文件用途**：Agent 的对话上下文在进程冷启动后会失效。
> 本文件是跨重启的"记忆载体"，记录重构进度、关键决策与下一步。
>
> **重启后恢复流程**：
> 1. `cat REFACTOR_PLAN.md` 读取本文件
> 2. `git log --oneline -5` + `git status` 核实实际代码状态
> 3. **以代码为准，不盲信本文件**；发现差异则修正本文件
> 4. 从"下一步"继续

---

## 当前状态

| 项 | 值 |
|---|---|
| 当前阶段 | **阶段 2 已完成，待用户验收** |
| 分支 | `feature/async-event-driven` |
| 回滚锚点 | `v0.7.2-pre-async`（main 分支重构前状态） |
| 最后更新 | 阶段 2 完成时 |

---

## 总计划

- [x] **阶段 0**：打标签 + 建分支 + 写本计划文件
- [x] **阶段 1**：事件总线（可热重载，行为等价）
- [x] **阶段 2**：内核异步化（冷重启）
- [ ] **阶段 3**：中断事件化（冷重启）
- [ ] **阶段 4**：并发工具执行
- [ ] **阶段 5**：bash 原生异步化（独立一轮，暂不做）

---

## 已完成

### 阶段 0（完成）
- 打标签 `v0.7.2-pre-async`（main 分支重构前状态，回滚锚点）
- 建分支 `feature/async-event-driven`
- 创建本文件 `REFACTOR_PLAN.md`

---

## 进行中

（无。阶段 2 已完成，等待用户验收后进入阶段 3。）

### 阶段 1：事件总线（已完成）

**目标**：把 `chat()` 里的回调穿透（`on_tool_call` / `on_tool_output`）改为事件发布/订阅。
**主循环结构不动，行为逐字节等价。不碰异步。**

**改动清单**：
- [x] 新增 `core/events.py`：Event 基类 + 9 种事件类型 + EventBus（emit / emit_sync）
- [x] 改 `core/assistant.py`：`__init__` 建 `self.bus`；`chat()` / `_stream_model()` /
      `_on_tool_output()` 接入事件发布
- [x] 改 `cli/repl.py`：新增 `register_event_subscribers()`，`run_repl` 启动时注册
- [x] 回归测试：真实对话（回调路径 + 事件路径）均通过
- [x] 修复：`reload_code()` 保留 bus 实例与订阅者
- [x] 打 tag `v0.7.3-events`

**关键实现细节（阶段 2 需注意）**：
- **互斥规则**：`chat()` 传入临时回调时走回调、不发事件；未传回调时发事件。
  这是为保持阶段 1 行为逐字节等价而设的过渡设计。
  **阶段 2 应移除回调参数，统一走事件总线。**
- `EventBus.emit_sync` 已能识别 async 订阅者（会警告但不 await）；
  阶段 2 改用 `await bus.emit(...)` 后 async 订阅者自动生效。

---

## 已完成（续）

### 阶段 2：内核异步化（已完成）

**目标**：`chat()` → `async def achat()`，模型调用改用 `astream` / `ainvoke`。

**改动清单（全部完成）**：
- [x] `core/assistant.py`：`chat` → `achat`；`_stream_model` → `_astream_model`（用 `self.model.astream`）
- [x] `core/assistant.py`：`_invoke_model` → `_ainvoke_model`（用 `self.model.ainvoke`）
- [x] `core/assistant.py`：**删除 `_run_interruptible()`**（约 90 行线程化调用器）
- [x] `core/assistant.py`：新增 `_await_with_interrupt()`——原生 asyncio 版中断/超时等待器
      （把调用包成 Task，50ms 轮询中断标志 + 首字节/整体超时，超时后 cancel）
- [x] `core/assistant.py`：**移除 `on_tool_call` / `on_tool_output` 回调参数**，统一走事件总线
- [x] `core/assistant.py`：事件派发从 `emit_sync` 改为 `await self.bus.emit(...)`
- [x] `core/assistant.py`：`_rollback` 改 async，并发布 `RollbackEvent` / `InterruptEvent`
- [x] `core/assistant.py`：`_confirm_command` 改用同步版 `_read_input_sync`
      （确认发生在 run_bash 的工作线程中，无事件循环）
- [x] `core/events.py`：新增 `emit_threadsafe()` + `bind_loop()`——工作线程事件投递回循环
- [x] `tools/builtin.py`：`run_bash` 改 `async def`，内部 `asyncio.to_thread(bash.run, ...)`
- [x] `tools/builtin.py`：三个内置工具加 `_concurrency=False` 元数据（为阶段 4 铺路）
- [x] `cli/repl.py`：`run_repl` → `async def arun_repl`；`_read_input` → `await prompt_async(...)`
- [x] `cli/repl.py`：新增 `_read_input_sync`（供工作线程确认流程）
- [x] `cli/repl.py`：`arun_repl` 启动时 `bus.bind_loop(asyncio.get_running_loop())`
- [x] `Assistant.py`：`main()` → `asyncio.run(arun_repl(assistant))`
- [x] 回归测试（见下）+ 打 tag `v0.7.4-async-core`

**回归测试结果（全部通过）**：
- 真实模型端到端：流式 + run_bash 工具调用 + 结果回填 + 多轮上下文 ✅
- 事件顺序：UserMessage → ModelStart → ToolCall(名) → ModelEnd → ToolCall(参数)
  → ToolResult → ModelStart → ModelChunk → ModelEnd ✅
- Esc 中断：流被 cancel，用户消息回滚，历史干净 ✅
- 超时：首字节超时触发，回滚，报错清晰 ✅
- `emit_threadsafe`：工作线程事件正确投递到主循环线程 ✅
- 热重载：bus 实例、loop 绑定、订阅者全部保留，重载后 achat 正常 ✅
- 安全策略：CONFIRM 允许/拒绝、DENY 拦截均正常 ✅
- 交互式确认（stdin）：工作线程中 `_read_input_sync` 正常 ✅
- install_skill / reload_self 工具：正常 ✅

**关键实现细节（阶段 3 需注意）**：
- 中断/超时统一由 `_await_with_interrupt` 处理：把协程包成 Task，
  轮询 `ctrl.is_set()` 与超时阈值，命中则 `task.cancel()`。
- `InterruptEvent` 只由 `_rollback` 发布（避免重复）。
- `run_bash` 的输出回调在工作线程中执行，必须用 `bus.emit_threadsafe`；
  该路径依赖 `bus.bind_loop()` 已绑定事件循环（由 `arun_repl` 完成）。
- 热重载保留 bus 实例，因此 loop 绑定与订阅者不会丢失（已验证）。

---

## 下一步

**等待用户验收阶段 2。** 验收通过后进入阶段 3：

### 阶段 3：中断事件化（需冷重启）

**目标**：把 Esc 中断从"轮询标志"改为"事件驱动"，并解决中断竞态。

**改动清单（待细化）**：
- [ ] 中断竞态：事件队列里残留的旧事件在中断后需丢弃
      （考虑用 `session_id` 或 generation 计数器标记，Event 基类已预留 `session_id` 字段）
- [ ] 评估是否把 `InterruptController` 的轮询改为 asyncio 事件/信号驱动
- [ ] 回归测试 + 打 tag `v0.7.5-interrupt-events`

---

## 关键决策记录（避免重启后遗忘）

1. **bash 暂不做大改动**：阶段 2 保持 `PersistentBash` 同步实现不变，
   用 `asyncio.to_thread` 桥接到异步世界。bash 是唯一保留同步中断语义
   （`ctrl.is_set()` + SIGINT）的模块。原生异步化留到阶段 5。

2. **工具并发**：确定要支持。工具注册需带"可并发"元数据：
   - `run_bash` / `install_skill` / `reload_self` → **不可并发**（共享 shell / 改注册表 / 改自身状态）
   - 技能库纯计算函数 → 默认**可并发**
   - 并发执行后必须**按原 tool_calls 顺序重排结果**，否则模型困惑

3. **事件派发策略**：顺序 `await`，**不并发派发**（保证输出不乱序）。

4. **异步化方式**：用户明确要求用原生 asyncio（`astream` / `ainvoke`），
   不用 `run_in_executor` 包装。但 bash 例外（见决策 1）。

5. **计划文件进 git**：`REFACTOR_PLAN.md` 不加入 `.gitignore`，
   跟随分支走，回退代码时计划一起回退。

6. **安全禁区**：`security/` 不改、不绕过。与本次重构无关。

---

## 已知问题 / 待办

- ~~阶段 2 后热重载机制可能与 async 不兼容（事件循环引用失效）~~
  → **已解决**：热重载保留 bus 实例，loop 绑定与订阅者随之保留（已验证）。
- 阶段 3 中断竞态：事件队列里残留的旧事件在中断后需丢弃
  （考虑用 `session_id` 或 generation 计数器标记；Event 基类已预留 `session_id`）。

---

## 回滚方法

```bash
# 回退到重构前（main 分支状态）
git checkout v0.7.2-pre-async

# 或放弃整个分支
git checkout main && git branch -D feature/async-event-driven
```
