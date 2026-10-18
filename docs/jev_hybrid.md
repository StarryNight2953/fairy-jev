# FAIRY LLM、Jev 与同步混合 Agent

本阶段只比较 `scenario_l1_three_cultivar_hn58_action_ready_irrigation`，实现名为 `hybrid_sync_v1`。LLM 周期性整理任务理解、证据与条件性计划，Jev 从完整工具菜单和既有参数候选中作出实际选择。K=4 是本次固定工程配置，尚无证据说明它最优。

本文是 2026-10-18 的实现与复现说明。三个架构的离线闭环、证据校验和动作回放已经通过；真实三组比较受外部条件阻塞。H6 smoke 中纯 Jev 的首次真实请求返回 HTTP 451：`Typesafe is not available in your region.`，没有产生模型选择或工具执行；另外两组因缺少 `DEEPSEEK_API_KEY` 未发出请求。正式三组共九个样本尚未实际执行，当前为 0/9。不能把这次服务访问失败解释为 Jev 任务策略失败，也不能从离线 scripted 成功推断真实混合 Agent 的收益。

本次已实际消耗一个 live episode、一次 HTTP 尝试、零次工具执行；没有可用 token usage，费用未知。该失败原始 trace 和响应保留，后续开发修复诊断或增加访问门禁不会改写它。正式标签与最终提交身份以完成冻结后的记录为准；本节结果来自下面列出的真实 smoke 与 offline 证据。

原始工作目录 `/Users/ouyangkun/PHD/Project/Fairy-Jev` 的用户修改、凭据及历史 run 保留不动。开发目录为 `/Users/ouyangkun/PHD/Project/Fairy-Jev-hybrid`，分支 `feat/llm-jev-hybrid-hn58`。已有实现先按白名单复制并逐文件校验，再冻结为提交 `f5bd5dc861f1eb5006ac1a8d089769f595620d1f`、本地标签 `baseline/llm-jev-20261018`。这不是混合实现提交，也不能追认成 2026-10-17 历史 live 使用的代码提交；后者仍记为未知。白名单包括 Jev、Engine hook、场景数据路径修复、必要配置与测试，并包括已提供的 XLSX 数据资产；没有复制 `env.sh`、虚拟环境或整个 runs 目录。小型身份索引见 [hn58_baseline.json](experiments/hn58_baseline.json)。

| architecture | 内部执行 | 理解与上下文 | 工具、参数与报告 |
|---|---|---|---|
| `llm_native` | FAIRY 已有 `farm_baseline_react` 的 function calling Loop | 原生 messages 和控制流程 | LLM 直接生成参数和报告文本；保留一次响应中的多个工具调用及原有串行顺序 |
| `jev` | 原 Jev 两阶段 Choice Loop，加公共预算与记录 hook | 原压缩后的 VisibleState | 从完整菜单选工具，再选有限完整实参组合；`factual_report()` 由代码根据可见事实构造报告 |
| `hybrid` | 同一个 Jev 两阶段执行流程，新增固定间隔更新 hook | 原始任务、最新 LLM packet、packet 后的新反馈 | 实际动作仍来自 Jev；候选及 `factual_report()` 与纯 Jev 共用，LLM brief 不进入事实账本 |

当前场景复核结果为注册 59 个工具、按原隐藏规则可见 58 个，第一阶段加 `FINISH` 后为 59 个选项。参数支持表为 20 个无参、34 个含必填参数、4 个仅可选参数；初始可见输入下 58 个策略均能生成候选，最大为 192 个。该有限目录不是连续参数空间的穷举。LLM 可以建议具体工具和数值，但建议不自动扩充候选或执行工具。参数建议不能由既有候选表达时，应记录覆盖问题。

| 文件 | 责任 |
|---|---|
| `fairy/agents/Jev/hybrid/agent.py` | `HybridJevAgent`，更新调度、请求投影与明确失败终止 |
| `fairy/agents/Jev/hybrid/context.py` | packet 验证、可见事件边界、冻结投影、请求大小检查 |
| `fairy/agents/Jev/hybrid/updater.py`、`prompt.md` | 复用现有 LLM provider 生成材料，最多一次重试或格式修复；通用提示不含本场景金标行动序列 |
| `fairy/agents/Jev/agent.py`、`tools.py` | 两阶段 Choice、真实执行与可选预算 hook；不带新架构参数的入口保持纯 Jev |
| `fairy/agents/Jev/candidates.py`、`state.py` | 原候选构造、原可见事实与报告来源；没有向这些组件注入 brief |
| `fairy/experiments/agent_comparison.py` | offline、smoke、formal、report 入口及固定区块顺序 |
| `fairy/experiments/episode.py` | 新建独立场景、身份与配置、产物、统一验收、回放连接 |
| `fairy/experiments/native.py` | 包装既有原生 Agent 的实际边界，不重写其内部规划、function calling 或参数生成 |
| `fairy/experiments/budget.py`、`transports.py` | episode 截止、持久总预算、真实 transport 尝试计数、剩余时间约束 |
| `fairy/experiments/evaluation.py`、`verification.py` | 版本化语义验收、trace 一致性和 packet/state 前缀重建 |
| `configs/jev/hn58_hybrid_sync_k4.json` | 单次运行有效配置，包含原有限候选目录 |
| `configs/experiments/hn58_three_agents_v1.json` | 三组次序、种子、批次身份、持久预算及正式代码标签名称 |

每轮先检查停止条件和预算，再按真实原生工具尝试数决定是否更新。正常更新发生在计数 0、4、8、…、36；工具尝试上限 40 时不会在 40 后刷新。进入工具后抛错仍计一次尝试；无候选、参数拒绝等执行前失败不计工具尝试，也不触发额外更新。混合组连续执行前失败由决策轮、请求与时间上限约束；纯 Jev 保留其原有重复参数失败终止策略。

LLM 初次更新读取公开任务、公开约束、已交付的初始观察和完整工具能力。以后读取固定任务、上一版 packet、上次覆盖点后的完整新事件，以及上一版实际引用过的原文证据。当前实现只把 trace 中的 `tool_end`、`environment_notification`、`controller_feedback` 作为动态可见事件；模型响应、候选生成记录、私有验收和 checkpoint 内部状态不进入这个证据集合。

模型只生成 `brief` 和整数 `evidence_refs`。运行器填写 `packet_id`、版本、工具计数、实际 `input_event_ids`、边界 `covers_through_event_id` 和来源请求 ID。边界只表示当次输入的时间范围，精确事件集合另行保存；没有重发的旧事件 ID 也明确记录。合法引用仅证明该事件真实提供给模型，不证明 LLM 的解释正确。

每轮决策冻结一个投影：原始任务和系统约束、最新 packet、该 packet 后的新原始反馈、当前时间与预算。更新成功后替换旧 packet；不会把所有旧 packet 和整个 VisibleState 再附加一遍。新的执行结果与通知立刻进入下一轮输入，不等待 K。工具 Choice 与参数 Choice 使用同一 packet 和观察截止点；两者之间收到的通知进入下一轮。候选生成读取同轮单独冻结的原 VisibleState，因此材料中的计划或推断不会被当作已观察事实。`FINISH` 在混合请求中表示整个原始任务完成，没有子任务 FINISH 或控制器自动补查。

请求不做静默选项删减。Jev 完整请求的工程上限为 60,000 UTF-8 bytes 且 60,000 字符，updater 输入为 120,000 bytes，brief 为 4,000 字符。这些是本地序列化长度代理，服务端实际 token 数以返回 usage 为准；不能把它们描述为精确 tokenizer 约束。超出配置时记录各组成部分并停止 `state_budget_exceeded`。updater 输出最多 2,048 tokens，映射到本项目 provider 已支持的 `max_tokens` 参数。输出无效或瞬时故障允许一次修复或重试，两者合计最多两次实际 HTTP 尝试；失败停止 `llm_update_error`，不退回纯 Jev或继续旧材料。

本次 LLM 配置沿用现有默认：DeepSeek、`deepseek-chat`、`https://api.deepseek.com`、temperature 0.1、max output 2,048、`parallel_tool_calls=true`。updater 与原生组采用同一模型和端点；updater 请求不传 FAIRY function tools，由自己的结构化材料提示约束输出，并关闭 SDK 嵌套自动重试，重试由 updater 统一控制。原生组保留原 SDK/Agent 重试路径，但每次实际 transport 尝试都受预算计数。Jev 固定官方 `typesafe-sdk==0.7.5`、`jev-1.13.0`、`https://api.typesafe.ai`，保持 `TypeSafeClient.system_one()`、`Choice` 及零 SDK 自动重试。

| 预算 | 本次实际有效值 |
|---|---:|
| 每个 episode 原生工具尝试 | 40，进入工具即计，包括异常 |
| Jev/混合决策轮 | 40；保留原实现较严上限，设计请求值 60 另存配置 |
| Jev/混合 Choice 逻辑调用 | 80；每个工具及参数 Choice 分别计数 |
| 每个 episode 全部模型 HTTP 尝试 | 120，包括失败、重试、修复 |
| Jev HTTP 尝试 | 100，同时受 80 Choice 和总 120 次限制 |
| 混合正常更新／updater 实际尝试 | 最多 10／20 |
| episode 墙钟 | 600 秒；保留原有较严值，设计请求值 900 另存配置 |
| 全任务 live | 最多 12 个 episode、1,440 次 HTTP 尝试 |

模拟等待 7,200 秒只改变模拟时间，不记作 7,200 秒真实延迟。外层 POSIX deadline 约束整个 episode；HTTP 各阶段 timeout 不超过剩余时间。停止异常绕过一般 SDK/Agent 重试处理。工具在截止时被中断，记录已经进入执行以及副作用未知，不自动重发，也不伪造完成的 Workflow 节点。

正式计划是三个区块、每组共三次，区块内顺序分别为 `llm_native → jev → hybrid`、`jev → hybrid → llm_native`、`hybrid → llm_native → jev`，种子分别为 1904、1905、1906。smoke 每组最多一次，单独记录，不替代正式样本。场景、checkpoint、初始时钟、权限、候选与验收版本共用；每次创建全新环境和记忆，组间不共享观察。seed 不能保证远程模型采样相同，模拟时钟仍存在墙钟派生的亚秒推进。本次外部阻塞未解除，九个正式样本不发起额外付费请求；blocked 记录不计作已执行的失败任务样本。

独立验收由 `hn58_semantic_v2` 执行，保持五项要求：灌溉前观察、目标范围与累计剂量、配额消耗、等待响应后的完整复查、符合条件的最终报告。原生 validator 结果另外保留；它不能覆盖独立失败。

v1 仍保留在 `fairy/agents/Jev/evaluation.py`，没有修改历史 run。v2 位于 `fairy/experiments/evaluation.py`，每次结果包含 `legacy_evaluation_v1.task_success` 和旧 assertions，说明以下交付路径修正：

- 灌溉前的天气、配额、土壤与目标区检查按实际返回字段、覆盖范围及时间验证；请求过某范围不等于确实观察过它。
- 公开任务要求“等待响应后复查”，没有指定必须调用一个唯一工具。v2 接受等待后真正覆盖目标区的垄状态或土壤传感器分区返回，并可按时间顺序合并有效覆盖；不能用灌溉前的读数证明灌溉后复查。v1 曾要求更特定的范围查询路径。
- 原生 LLM 的最终无工具 assistant 消息可作为报告来源，和 `send_message_to_user` 一样，必须出现在完整复查之后。v1 只接受报告工具路径。

范围、剂量、配额、实际响应与等待的物理断言仍复用 v1。传感器覆盖判定依赖其实际返回的 `ridge_start/ridge_end` 和有限含水量数值；这不是证明区间内每垄都被独立精确测量。最终文本检查采用确定性关键词与未完成声明规则，不是通用语义真实性判定器，也未逐条验证报告中的每个数值。这些限制与旧判定同时披露，正式三组使用同一 v2，Agent 无权读取私有结果。

每次 episode 保存 `manifest.json`、`trace.jsonl`、`workflow.json`、`evaluation.json`、`metrics.json`、`verification.json`、`files.sha256`，有动作时另有 `replay/`。manifest 记录实际代码提交、工作树状态、模块导入路径、脱敏有效配置与哈希、SDK/锁文件/提示/候选/场景/checkpoint/必要数据资产身份。调试运行带工作树差异，不能仅凭 HEAD 冒充干净代码。正式运行前要求代码、配置、提示及验收位于同一个干净提交 R，并建立不可移动的实验标签；之后报告提交 R2 不会改变 run 的 `code_commit`。

trace 区分逻辑请求与真实 HTTP attempt，记录五种角色、输入、响应、usage 和来源链接。费用缺少可核验价格或账单时标记 unavailable，缺失 tokens 用 null。模型返回内容不称为隐藏思维链。LLM 材料在 trace 中可重建到 Jev 请求；动作回放不调用模型，并比较实际副作用、配额和 pending 状态。`replay/evaluation.json` 保留既有回放/v1 结果，`replay/evaluation_semantic_v2.json` 保存新语义验收。回放一致说明执行记录可复现，不证明策略正确。

H6 的 [smoke_index.json](../runs/agent_comparison/hn58-three-agents-v1/smoke_index.json) 保存三个预定槽位的实际记录：

| architecture | 实际请求／工具 | `live_integration_success` | `task_success` | 实际结果 |
|---|---:|---|---|---|
| `llm_native` | 0／0 | null | null | `blocked`，缺少 `DEEPSEEK_API_KEY` |
| `jev` | 1／0 | false | null | `integration_error`，首次 tool Choice 收到 HTTP 451 |
| `hybrid` | 0／0 | null | null | `blocked`，缺少 `DEEPSEEK_API_KEY` |

纯 Jev 失败 run 为 `20261018T094403_jev_live_3ec48d66`，来自干净提交 `85ac64ff1a3897a9db72a35e5cf9d0476b9404c2`。其 [trace](../runs/agent_comparison/hn58-three-agents-v1/smoke/20261018T094403_jev_live_3ec48d66/trace.jsonl) 的 event 6、`http_0001` 保留原始响应 `{"title":"Typesafe is not available in your region.","status":451}`。原始 evaluation 的 `failure_layer` 为 null；不能事后修改原始文件补充诊断。当前可从 HTTP 证据定位到 provider 的区域访问拒绝，尚未获得任何工具选择答案。这一 run 的 episode 墙钟约 0.338 秒、transport 响应约 0.214 秒，仅反映访问拒绝的耗时，不可用于三组任务延迟比较。`input_tokens`、`output_tokens` 均为 null，usage 与费用均 unavailable。trace 完整性通过表明失败证据关联齐全，不表示集成通过；零实际工具执行的动作 replay 为 not applicable。

H8 可以核对的机制证据来自 [offline_index.json](../runs/agent_comparison/hn58-three-agents-v1/offline_index.json)：三组均用 scripted 响应驱动真实 FAIRY 环境，各执行 10 次工具，独立 scripted 任务验收、trace 校验和动作回放通过，实际模型 HTTP 为零。它验证接口与环境闭环，不构成真实 LLM/Jev 的能力样本。

混合离线 run `20261018T093652_hybrid_scripted_7974a11f` 的 [trace](../runs/agent_comparison/hn58-three-agents-v1/offline/20261018T093652_hybrid_scripted_7974a11f/trace.jsonl) 保存三版 scripted 材料：

| packet | 创建时真实工具次数 | 本次实际提交原始事件 ID | 后续关联的工具执行 |
|---|---:|---|---|
| `packet_0001` | 0 | 空集；公开任务与能力已提供 | 天气、土壤、目标范围、配额，共 4 次 |
| `packet_0002` | 4 | 14、24、39、49 | 灌溉、推进模拟时间、范围复查、土壤复查，共 4 次 |
| `packet_0003` | 8 | 14、24、39、49、67、82、97、107 | 配额复查、报告，共 2 次；随后 FINISH |

第三版输入包括四条先前引用过的原文和四条新反馈，并未只传 ID 而声称传入原始内容。每次 Choice 的 packet、状态截止点及候选来源可从 trace 复核；这里的动作次序由 offline policy 预先决定，材料也是 `ScriptedUpdater` 生成的通用 fixture。上述关联只证明调度、反馈替换与记录机制工作，不能证明真实 LLM 形成了该计划、真实 Jev 采纳了建议，或材料复用带来了有效任务进展。H6/H7 当前阻塞，因此没有真实混合行为轨迹可用于这三种归因。

以下命令在开发 worktree 使用。`--help` 和 offline 不访问模型；live 命令需要已有凭据，smoke/formal 共用 `runs/agent_comparison/live_budget_20261018.json`，不要删除该文件或改名绕过额度。常规 Jev 入口不带 `--architecture` 时仍为纯 Jev。

```bash
cd /Users/ouyangkun/PHD/Project/Fairy-Jev-hybrid
/Users/ouyangkun/PHD/Project/Fairy-Jev-hybrid/.venv/bin/python -m fairy.agents.Jev --help
/Users/ouyangkun/PHD/Project/Fairy-Jev-hybrid/.venv/bin/python -m fairy.experiments.agent_comparison --help

/Users/ouyangkun/PHD/Project/Fairy-Jev-hybrid/.venv/bin/python -m fairy.agents.Jev \
  --architecture hybrid --mode scripted \
  --config /Users/ouyangkun/PHD/Project/Fairy-Jev-hybrid/configs/jev/hn58_hybrid_sync_k4.json \
  --output-dir /Users/ouyangkun/PHD/Project/Fairy-Jev-hybrid/runs/jev/hybrid_scripted

/Users/ouyangkun/PHD/Project/Fairy-Jev-hybrid/.venv/bin/python -m fairy.experiments.agent_comparison \
  --config /Users/ouyangkun/PHD/Project/Fairy-Jev-hybrid/configs/experiments/hn58_three_agents_v1.json --stage offline
```

真实运行使用已有凭据机制，只加载环境，不显示文件内容或密钥。以下是已实现接口，不是要求在当前区域访问阻塞下重新探测。纯 Jev smoke 槽位已经消耗，持久预算不能通过重复命令、换目录或重命名清零。缺少凭据或 provider 可用性的组记录 blocked，不把未调用结果写成失败率样本。

```bash
set -a
source /Users/ouyangkun/PHD/Project/Fairy-Jev/env.sh
set +a

/Users/ouyangkun/PHD/Project/Fairy-Jev-hybrid/.venv/bin/python -m fairy.experiments.agent_comparison \
  --config /Users/ouyangkun/PHD/Project/Fairy-Jev-hybrid/configs/experiments/hn58_three_agents_v1.json --stage smoke

# 必须已建立配置指定的实验标签，并处于对应干净提交，才能执行 formal。
/Users/ouyangkun/PHD/Project/Fairy-Jev-hybrid/.venv/bin/python -m fairy.experiments.agent_comparison \
  --config /Users/ouyangkun/PHD/Project/Fairy-Jev-hybrid/configs/experiments/hn58_three_agents_v1.json --stage formal

/Users/ouyangkun/PHD/Project/Fairy-Jev-hybrid/.venv/bin/python -m fairy.experiments.agent_comparison \
  --config /Users/ouyangkun/PHD/Project/Fairy-Jev-hybrid/configs/experiments/hn58_three_agents_v1.json --stage report
```

独立混合 live 入口也支持 `--architecture hybrid --mode live`，但本次有界批次应使用上面的 smoke/formal 入口保留全任务持久计数，不另加付费连通性探针或额外 live episode。

以下是已存在 scripted 轨迹的动作回放命令。它不会请求模型；source manifest 决定 checkpoint、时钟和种子。传入的配置与 source 不一致会拒绝，重放其他区块时应使用其一致配置。

```bash
/Users/ouyangkun/PHD/Project/Fairy-Jev-hybrid/.venv/bin/python -m fairy.agents.Jev \
  --mode replay \
  --config /Users/ouyangkun/PHD/Project/Fairy-Jev-hybrid/configs/jev/hn58_hybrid_sync_k4.json \
  --workflow /Users/ouyangkun/PHD/Project/Fairy-Jev-hybrid/runs/jev/hybrid_scripted/20261018T093440_hybrid_scripted_03eb76f9/workflow.json \
  --output-dir /Users/ouyangkun/PHD/Project/Fairy-Jev-hybrid/runs/agent_comparison/manual_replay
```

新 worktree 的复现必须从已经存在的实际实验标签开始；下例中的标签名来自配置，先用 `rev-parse --verify` 检查其实际提交。不要从基线标签冒充最终混合实现。此处用于离线复现；继续真实批次应使用原开发 worktree 的同一持久预算，不在新目录重新开始一份额度。

```bash
git -C /Users/ouyangkun/PHD/Project/Fairy-Jev-hybrid rev-parse --verify refs/tags/experiment/hn58-three-agents-v1
git -C /Users/ouyangkun/PHD/Project/Fairy-Jev-hybrid worktree add --detach \
  /Users/ouyangkun/PHD/Project/Fairy-Jev-hn58-reproduce experiment/hn58-three-agents-v1
cd /Users/ouyangkun/PHD/Project/Fairy-Jev-hn58-reproduce
/Users/ouyangkun/tools/miniconda/install_dir/miniconda3/envs/are/bin/uv sync --locked --extra jev \
  --python /Users/ouyangkun/PHD/Project/Fairy-Jev-hybrid/.venv/bin/python
.venv/bin/python -c 'import fairy, importlib.metadata; print(fairy.__file__); print(importlib.metadata.version("typesafe-sdk"))'
.venv/bin/python -m fairy.experiments.agent_comparison \
  --config /Users/ouyangkun/PHD/Project/Fairy-Jev-hn58-reproduce/configs/experiments/hn58_three_agents_v1.json --stage offline
```

本次重新执行的 H0 基线回归为 165 passed；这是冻结已有代码之后的一次新验证，记录在 `runs/agent_comparison/checkpoints/H0/baseline_tests.txt`。H3/H5 聚焦回归为 88 passed in 0.41s，包含 17 项混合 Loop 和 11 项预算测试，命令及输出位于 `runs/agent_comparison/checkpoints/H3/regression.txt`。完整阶段命令、修改文件与验证结果保存在 `runs/agent_comparison/checkpoints/H0` 到 `H8` 的实际 checkpoint 文件；尚未完成的阶段不由本文补造结果。

最终报告应逐组区分集成覆盖、原生 validator、独立任务结果、首次有证据的问题、实际 HTTP/工具次数、整段墙钟和各角色 usage。三个重复只是小样本描述，成功或失败都进入预定样本。不能用 Jev 单次调用延迟替代混合系统的总延迟，也不能把混合成功归因于 Jev 单独完成了理解和全局规划。
