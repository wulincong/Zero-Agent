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
| 当前阶段 | **阶段 5 已完成，待用户验收** |
| 分支 | `feature/async-event-driven` |
| 回滚锚点 | `v0.7.2-pre-async`（main 分支重构前状态） |
| 最后更新 | 阶段 5 完成时 |

---

## 总计划

- [x] **阶段 0**：打标签 + 建分支 + 写本计划文件
- [x] **阶段 1**：事件总线（可热重载，行为等价）
- [x] **阶段 2**：内核异步化（冷重启）
- [x] **阶段 3**：中断事件化（冷重启）
- [x] **阶段 4**：并发工具执行
- [x] **阶段 5**：bash 原生异步化

---

## 已完成

### 阶段 0（完成）
- 打标签 `v0.7.2-pre-async`（main 分支重构前状态，回滚锚点）
- 建分支 `feature/async-event-driven`
- 创建本文件 `REFACTOR_PLAN.md`

---

## 进行中

（无。阶段 5 已完成，等待用户验收。）

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

## 已完成（续）

### 阶段 3：中断事件化（已完成）

**目标**：解决中断竞态，让"一次 Esc 只被响应一次"，并丢弃中断后残留的旧事件。

**竞态分析（实现前已用测试复现确认）**：
- **竞态 A（标志残留）**：bash 中断后 `ctrl` 标志仍为 set，回到主循环
  `for call` 开头 `is_set()` 再次为真 → 重复 rollback + 重复"已中断"提示。
- **竞态 B（事件残留）**：`run_bash` 工作线程 `emit_threadsafe` 投递的
  ToolResultEvent 是 fire-and-forget；若主循环已因中断回滚并进入下一轮，
  该旧事件仍会被派发 → 打印出已被丢弃的工具输出，污染新会话。
- **竞态 C（残缺结果入历史）**：bash 被中断时返回 `"[已中断]..."` 字符串
  （非异常），若只有一个 tool_call 则不会被后续 `is_set()` 拦截，
  残缺结果被写入 memory → 模型下一轮看到语义混乱的历史。

**改动清单（全部完成）**：
- [x] `runtime/interrupt.py`：新增 `consume()`——加锁的"读取并清除"原子操作，
      取代"`is_set()` + 事后 `clear()`"的组合，保证一次中断只被消费一次
- [x] `core/events.py`：新增**会话代次**机制——`new_generation()` / `generation`
      / `stamp()` / `_is_stale()`；`emit` 与 `emit_sync` 派发前丢弃过期代次事件
- [x] `core/assistant.py`：`achat()` 开头 `ctrl.consume()` + `bus.new_generation()`
- [x] `core/assistant.py`：`_await_with_interrupt` 与工具循环改用 `ctrl.consume()`
- [x] `core/assistant.py`：新增 `_is_interrupt_result()`，工具返回中断标记时
      回滚本轮而非写入历史（解决竞态 C）
- [x] `runtime/bash.py`：中断检测改用 `consume()`；新增 `INTERRUPT_MARKER`
      常量，中断返回值带结构化前缀
- [x] 回归测试（见下）+ 打 tag `v0.7.5-interrupt-events`

**回归测试结果（全部通过）**：
- `consume()` 原子语义：未置位 False / 置位 True / 消费后清除 / 二次消费 False ✅
- 代次过滤：旧代次事件被丢弃、新代次正常派发、未开启代次不过滤（兼容）✅
- `emit_threadsafe` 跨代次：工作线程旧代次事件被丢弃 ✅
- bash 中断：返回值带 `INTERRUPT_MARKER`，标志被消费 ✅
- 内核识别中断标记：正确识别、普通输出/非字符串不误判 ✅
- 无重复 rollback：bash 消费后主循环不再重复消费 ✅
- 端到端（真实模型）：普通对话 + 工具调用 + 事件顺序 + 代次递增 ✅
- 端到端中断（工具阶段）：恰好一次 InterruptEvent、历史回滚干净、下一轮正常 ✅
- 端到端中断（模型阶段）：同上，且无重复中断 ✅
- 热重载：bus 实例、代次状态、订阅者全部保留，重载后 achat 正常 ✅
- `security/` 无任何改动 ✅

**关键实现细节（阶段 4 需注意）**：
- 中断语义已从"持续状态"改为"一次性事件"：**任何中断判断都应使用
  `ctrl.consume()`**，只有纯只读判断（如 `_astream_model` 的 error 分支
  判断"用户是否已按 Esc 以决定是否重发"）才用 `is_set()`。
- 代次过滤是"软过滤"：未开启代次或事件未打 `session_id` 时不过滤，
  因此单元测试直接 `emit` 不受影响。
- 阶段 4 并发执行工具时，**每个工具的结果事件都应带当前代次**
  （用 `bus.stamp()`），否则中断后残留的并发结果会污染下一轮。

---

## 已完成（续）

### 阶段 4：并发工具执行（已完成）

**目标**：同一轮内多个 tool_calls 并发执行，但结果按原顺序回填。

**改动清单（全部完成）**：
- [x] `core/assistant.py`：新增模块级 `_tool_is_concurrent(tool)`——读取工具
      `_concurrency` 元数据（显式 False 不可并发；未标注的技能默认可并发）
- [x] `core/assistant.py`：新增 `_run_tool_calls(tool_calls, ctrl)`——按原序扫描，
      把**连续的可并发工具**聚成批次用 `asyncio.gather` 并发执行；
      不可并发工具（run_bash / install_skill / reload_self）单独串行执行
- [x] `core/assistant.py`：新增 `_gather_with_interrupt(tasks, ctrl)`——并发等待
      一组任务，50ms 轮询中断，命中则取消全部未完成任务并抛 `_Interrupted`
- [x] `core/assistant.py`：新增 `_execute_one_tool(call, tool, ctrl)`——执行单个工具，
      返回 `(结果, 是否中断)`；只读共享状态、不改 memory，可安全并发调用
- [x] `core/assistant.py`：`achat()` 工具循环改为调用 `_run_tool_calls`，
      结果**按原 tool_calls 顺序**回填 memory
- [x] `core/assistant.py`：`_execute_one_tool` 中 ToolCallEvent 用 `bus.stamp()`
      打当前代次，避免中断后残留的并发结果事件污染下一轮
- [x] 回归测试（见下）+ 打 tag `v0.7.6-concurrent-tools`

**回归测试结果（全部通过）**：
- 并发元数据判定：run_bash/install_skill/reload_self=False，技能=True ✅
- 批次切分 + 按原序回填：[技能A, 技能B, run_bash, 技能A] 结果顺序正确 ✅
- 真实并发：3×0.3s 任务总耗时 0.30s（< 0.6s，确认并发）✅
- 不可并发串行：2×0.2s 任务总耗时 0.40s（>= 0.4s，确认串行）✅
- 并发批次中断：0.20s 内取消全部任务 ✅
- 批次前中断被消费（一次 Esc 只响应一次）✅
- 工具异常隔离：单个工具抛异常不影响同批其它工具，结果按序回填 ✅
- 未找到工具的错误处理 ✅
- 端到端（真实模型）：一轮内 3 个工具调用，结果按原序回填，事件代次正确 ✅
- 端到端中断：恰好 1 次 InterruptEvent + 1 次 RollbackEvent，历史干净，
  下一轮正常 ✅
- 热重载：bus 实例/订阅者保留，重载后并发执行 + 按序回填正常 ✅
- `security/` 无任何改动 ✅

**关键实现细节（阶段 5 需注意）**：
- 并发只发生在**相邻**的可并发工具之间：`[技能A, run_bash, 技能B]` 中
  A 与 B 不会并发（run_bash 把批次切断了）。这是刻意的保守设计，
  保证与串行工具的相对次序不被打破。
- `_execute_one_tool` 被设计为可安全并发：只读共享状态、不改 memory，
  结果通过返回值交给调用方按序回填。
- 并发批次被中断时，未完成的任务会被 `cancel()`；`_execute_one_tool` 中
  `asyncio.CancelledError` 直接向上传播（不吞掉），由 `_gather_with_interrupt`
  统一处理。
- 流式阶段的 ToolCallEvent（仅工具名、无参数）**不带代次**，属预期；
  只有参数聚合完成后的 ToolCallEvent（来自 `_execute_one_tool`）带代次。

---

## 已完成（续）

### 阶段 5：bash 原生异步化（已完成）

**目标**：把 `PersistentBash` 从"同步 + `asyncio.to_thread` 桥接"改为原生异步
（`asyncio.create_subprocess_exec` + 非阻塞读取），彻底移除线程池依赖。

**改动清单（全部完成）**：
- [x] `runtime/bash.py`：`subprocess.Popen` + `select` 轮询 → `asyncio.create_subprocess_exec`
      + `asyncio.wait_for(reader.read(...))` 轮询；`run()` → `async def arun()`
- [x] `runtime/bash.py`：子进程延迟到首次使用时在事件循环内创建（`_ensure_proc`）
- [x] `runtime/bash.py`：中断检测仍用 `ctrl.consume()`，命中后 `proc.send_signal(SIGINT)`
- [x] `runtime/bash.py`：新增 `_strip_exit_marker()`——剔除 stderr 中的 `__EXIT:N__` 行
      （异步一次性读取会把退出码行与哨兵一起读入，需显式剔除，否则污染输出）
- [x] `runtime/bash.py`：`_kill_proc()` 显式关闭 stdin/stdout/stderr 管道与 transport，
      并 `await asyncio.sleep(0)`，消除事件循环关闭后的析构告警
- [x] `runtime/bash.py`：`close()` 改 async；`_restart()` 改 async
- [x] `tools/builtin.py`：`run_bash` 直接 `await bash.arun(...)`，移除 `asyncio.to_thread`
- [x] `core/assistant.py`：`_confirm_command` 改 async（确认流程回到事件循环线程，
      直接 `await _read_input(...)`，不再需要线程桥接）
- [x] `core/assistant.py`：`_on_tool_output` 注释更新（回调现在运行在事件循环线程）
- [x] `cli/repl.py`：`assistant.bash.close()` → `await assistant.bash.close()`
- [x] `cli/repl.py`：删除已无调用者的 `_read_input_sync`
- [x] 回归测试（见下）+ 打 tag `v0.7.8-bash-async`

**回归测试结果（全部通过）**：
- 单元（15/15）：基本执行 / cd 状态保持 / export 状态保持 / 无换行输出 /
  stderr 合并 / 退出码行剔除 / 非零退出仍返回输出 / 超时触发 / 超时后会话重置 /
  DENY 拦截 / 中断生效 / 中断后标志被消费 / 中断后会话可用 /
  CONFIRM 异步回调被调用 / 无通道 CONFIRM 拒绝 ✅
- 端到端（12/12，真实模型）：工具调用 / 事件含 ToolCall+ToolResult /
  事件顺序 / 多轮上下文 / 中断返回提示 / 中断及时 / 恰好一次 InterruptEvent /
  恰好一次 RollbackEvent / 中断后下一轮正常 / 热重载成功 / 热重载后工具可用 ✅
- 并发（7/7）：run_bash/install_skill/reload_self 不可并发 / 两个 run_bash 串行 /
  结果按原序回填 / 未中断 / 端到端多 bash 调用 ✅
- 冷启动 REPL：`printf 'echo ...\nexit\n' | python Assistant.py` 正常启动、
  工具调用、输出折叠、退出 ✅
- `security/` 无任何改动 ✅

**关键实现细节**：
- 中断语义不变：仍是 `ctrl.consume()` 一次性消费 + SIGINT 打断前台命令。
- 确认流程从"工作线程 + 同步输入"变为"事件循环线程 + 异步输入"，
  因此 `_read_input_sync` 被删除。
- `emit_threadsafe` 保留（向后兼容），但 bash 事件现在直接在循环线程产生，
  会走其"同步派发"分支。
- 退出码行 `__EXIT:N__` 必须显式剔除：同步版用 select 分次读取时通常不会
  与哨兵同批到达，异步版 `read(65536)` 会一次性读入，故必须处理。

---

## 下一步

**等待用户验收阶段 5。** 全部 5 个阶段已完成，重构收尾。


---

## 关键决策记录（避免重启后遗忘）

1. **bash 异步化（阶段 5 已完成）**：`PersistentBash` 已改为原生异步
   （`asyncio.create_subprocess_exec` + 非阻塞读取），不再依赖线程池。
   中断语义保持：`ctrl.consume()` 一次性消费 + SIGINT 打断前台命令。
   确认回调随之改为 async（运行在事件循环线程）。

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
- ~~阶段 3 中断竞态：事件队列里残留的旧事件在中断后需丢弃~~
  → **已解决**：引入会话代次（`new_generation` / `stamp` / `_is_stale`），
  中断后残留的旧代次事件被事件总线丢弃；中断标志改用 `consume()` 原子消费。
- ~~bash 依赖线程池桥接（`asyncio.to_thread`），非原生异步~~
  → **已解决**：阶段 5 改为原生 asyncio 子进程，移除线程池依赖。

---

## 回滚方法

```bash
# 回退到重构前（main 分支状态）
git checkout v0.7.2-pre-async

# 或放弃整个分支
git checkout main && git branch -D feature/async-event-driven
```
