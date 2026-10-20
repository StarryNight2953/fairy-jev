# FAIRY LLM、Jev 与同步混合 Agent

## 2026-10-20：原生 LLM 切换为原生 CLI 启动和日志

新的 live `llm_native` 统一走 `python -m fairy.cli --agent`（与 `fairy-run` 同一入口），版本为 `fairy_native_cli_v1`。`episode.run_episode` 在创建比较框架的 fixture、TraceWriter、EpisodeRuntime 之前分流；`full_suite` 和 `agent_comparison` 都使用此入口。旧 observed adapter 仅供已有离线回归和历史证据解释，不再构造真实模型客户端。Jev 与 Hybrid 的运行路径保持原样。

子进程内由原生 CLI 创建模型、场景、Agent、Engine，执行原生 Loop 和原生验收，未安装 HTTP transport observer、工具包装器、比较框架 deadline 或重试策略。批量框架仅启动进程、转发显式终止信号、读取落盘文件。每次使用新目录，CLI 的工作目录也是独立空目录，避免其相对路径清理影响仓库中的历史 exports。启动命令、工作目录及非敏感环境覆盖记录在 `manifest.json`。

模型/provider/endpoint、temperature、输出 token 上限和 parallel tool calls 映射为原生 CLI 参数；1800 秒请求超时与 thinking 设置使用原生已支持的 `FAIRY_LLM_REQUEST_TIMEOUT_S`、`FAIRY_VLLM_ENABLE_THINKING`。认证直接使用原生 provider 的环境变量（本地 vLLM 无设置时由原生客户端使用 `EMPTY`）；不再调用 comparison credentials 适配器，也不把密钥放入命令或 manifest。场景 seed 和 checkpoint 初始时刻通过公开 `--scenario-kwargs` 传入，不再构建比较 fixture 后修改 Engine。

原生默认单场时限为 7200 秒、工具次数为 300；它们由原生 Loop 的检查机制执行，不保证强制打断在途请求。需要改动时在新配置中显式写 `native_cli.timeout_seconds` / `native_cli.max_tool_calls`，映射到 CLI 同名参数。比较配置中的 `experiment_limits`、`budgets` 不应用于原生进程；请求次数不再被比较框架拦截。SDK 和 Agent 重试遵循当前仓库原生实现。

每个新运行目录保留原生文件：

| 文件 | 来源与用途 |
| --- | --- |
| `<scenario>.run_report.json` | 原生报告：状态、时长、用量、验收、产物路径，优先阅读 |
| `<scenario>.runtime_log.jsonl` | 原生完整运行日志：messages、模型调用、工具调用和错误 |
| `<scenario>.progress.jsonl` | 原生运行期间追加的进度事件；不包含完整 messages |
| `<scenario>.agent_workflow.json` | 原生保存的工具执行工作流 |
| `console.log` | 原生 CLI 的 stdout/stderr |

外层 `manifest.json`、`evaluation.json`、`metrics.json` 仅用于定位与汇总，不覆盖上述文件。不生成比较格式 `trace.jsonl`，也不自动运行比较框架的 replay。CLI 失败或被中断时保留已产生的文件，不追加重跑。

原生 SDK 的真实 HTTP 尝试次数（含内部重试）未被观测，统一记为 `null`；原生报告的 `llm_call_count` 不能替代该计数。共享 ledger 保留历史数据并登记新 episode，将新增原生 HTTP 标为未知、累计 HTTP 标为不完整，不把未知使用量当作零。原生 `evaluation.validation` 完整保留；旧自定义任务断言与集成覆盖未运行，因此对应 `task_success` / `live_integration_success` 为 `null`。特别是 `Engine.evaluate` 对无 validator 结果也可能返回 success，不能直接当作严格任务成功。后续如需统一任务验收，应从保存的原生日志另做明确标识的离线分析。

此切换用于排除启动和观测层的干扰，尚不能证明历史延迟由该层造成。本次只做禁止联网的离线验证，没有重启已停止的实验。

## 以下为 2026-10-18 历史交付

用户随后指定本地 Qwen 服务，已另建配置和不可移动标签完成原生 LLM 的1次smoke及3次正式运行。最新结果见 [本地 Qwen 续跑报告](jev_hybrid_qwen.md)；下文保留最初 DeepSeek 配置阶段的历史交付事实。

本阶段只比较 `scenario_l1_three_cultivar_hn58_action_ready_irrigation`，实现名为 `hybrid_sync_v1`。LLM 周期性整理任务理解、证据与条件性计划，Jev 从完整工具菜单和既有参数候选中作出实际选择。K=4 是本次固定工程配置，尚无证据说明它最优。

本文是 2026-10-18 的实现与复现说明。三个架构的离线闭环、证据校验和动作回放已经通过；真实三组比较受外部条件阻塞。H6 smoke 中纯 Jev 的首次真实请求返回 HTTP 451：`Typesafe is not available in your region.`，没有产生模型选择或工具执行；另外两组因缺少 `DEEPSEEK_API_KEY` 未发出请求。正式三组共九个样本尚未实际执行，当前为 0/9。不能把这次服务访问失败解释为 Jev 任务策略失败，也不能从离线 scripted 成功推断真实混合 Agent 的收益。

本次已实际消耗一个 live episode、一次 HTTP 尝试、零次工具执行；没有可用 token usage，费用未知。该失败原始 trace 和响应保留，后续开发修复诊断或增加访问门禁不会改写它。实验标签 `experiment/hn58-three-agents-v1` 已冻结于 `89aa4e0e2491cff488428cef1941d1ba182c78d7`（R）。冻结后重新执行了三组 offline，并生成九个正式 blocked 记录；这些 manifest 均指向干净 R。最终报告提交 R2 只增加文档和索引，不移动实验标签，也不改写 smoke 使用的 `85ac64ff1a3897a9db72a35e5cf9d0476b9404c2`。修复后的 R 没有额外 live smoke 复测。

逐次结果、具体阻塞、版本和文件哈希位于 [交付索引](experiments/hn58_three_agents_v1_results.json)；完整运行产物仍保存在 `runs/agent_comparison/`，未批量提交大型 trace。R2 的实际哈希可用 `git log -1 --format=%H -- docs/experiments/hn58_three_agents_v1_results.json` 查询，不能将它当作运行代码版本。

[汇总文件哈希](../runs/agent_comparison/files.sha256) 关联实际证据；交付前校验了 276 个 run 产物哈希，未发现不一致。对新代码、配置、文档、测试和运行产物中的 1,139 个文件按当前已加载 API 密钥值检查，未发现匹配；检查记录只保存变量名称和结果，不包含密钥或片段。

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

模拟等待 7,200 秒只改变模拟时间，不记作 7,200 秒真实延迟。外层 POSIX deadline 约束环境初始化和模型／工具执行阶段；HTTP 各阶段 timeout 不超过剩余时间。验收、回放和产物落盘在执行截止之后独立完成，不保证其处理时间也被 600 秒 timer 中断。停止异常绕过一般 SDK/Agent 重试处理。工具在截止时被中断，记录已经进入执行以及副作用未知，不自动重发，也不伪造完成的 Workflow 节点。

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

冻结 R 后的混合 run `20261018T095447_hybrid_scripted_cc130068` 又验证了同一机制，详见 [逐包分析](../runs/agent_comparison/checkpoints/H8/frozen_packet_analysis.json)。三份材料后的工具／参数 Choice 数分别为 4／1、4／3、3／1，末包包含 FINISH，共 16 次决策；对应真实工具数为 4、4、2。packet_0002 更新后，event 67 的灌溉返回立即进入 event 69 的下一轮 state，期间没有再次更新材料。五对两阶段快照、十一轮新反馈检查和十六次状态重建均通过。真实模拟灌溉覆盖 43–63 垄、每垄 0.8 小时，等待 7,200 模拟秒后复查，配额使用量从 0 增至 1.3125，总额为 1.4。重复的范围、土壤、库存查询分别提供动作前后观察；没有重复修改动作。候选足以表达这条脚本路径，但未证明能覆盖任意模型建议。三包使用同一通用 scripted brief，因此不存在可归因于真实 LLM 理解或真实 Jev 遵循建议的证据；该片段没有发现执行或记录问题，真实链路的首个问题仍是服务访问层。

以下命令在开发 worktree 使用。`--help` 和 offline 不访问模型；live 命令需要已有凭据，smoke/formal 共用 `runs/agent_comparison/live_budget_20261018.json`，不要删除该文件或改名绕过额度。常规 Jev 入口不带 `--architecture` 时仍为纯 Jev。

纯 LLM 由统一入口中的 `llm_native` 槽位调用原生 `farm_baseline_react`；下面的 offline、smoke、formal 命令都按配置包含该槽位。`configs/experiments/hn58_three_agents_v1.json` 固定三种架构及顺序，单次 `run_episode(..., architecture='llm_native'|'jev'|'hybrid')` 是内部薄适配接口。原始 FAIRY CLI 仍可由 `.venv/bin/python -m fairy.cli --help` 查看，已实际核验；本次付费实验走统一入口，才能保留公共预算和身份记录。

```bash
cd /Users/ouyangkun/PHD/Project/Fairy-Jev-hybrid
/Users/ouyangkun/PHD/Project/Fairy-Jev-hybrid/.venv/bin/python -m fairy.agents.Jev --help
/Users/ouyangkun/PHD/Project/Fairy-Jev-hybrid/.venv/bin/python -m fairy.experiments.agent_comparison --help

# 纯 Jev 的既有 scripted 入口；不带 --architecture 仍采用 Jev。
/Users/ouyangkun/PHD/Project/Fairy-Jev-hybrid/.venv/bin/python -m fairy.agents.Jev \
  --mode scripted \
  --config /Users/ouyangkun/PHD/Project/Fairy-Jev-hybrid/configs/jev/hn58_irrigation.json \
  --output-dir /Users/ouyangkun/PHD/Project/Fairy-Jev-hybrid/runs/jev/scripted

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

独立混合 live 入口也支持 `--architecture hybrid --mode live --config configs/jev/hn58_hybrid_sync_k4.json --output-dir runs/jev/hybrid_live`；纯 Jev 的既有入口为 `--mode live --config configs/jev/hn58_irrigation.json --output-dir runs/jev/live`。这些模式通过 `--help` 和适配层验证，但本次有界真实批次只使用上面的 smoke/formal 命令，不能用旧独立入口绕过新增的全任务持久计数，也不另加付费连通性探针或额外 live episode。

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

本次重新执行的 H0 基线回归为 165 passed；这是冻结已有代码之后的一次新验证，记录在 `runs/agent_comparison/checkpoints/H0/baseline_tests.txt`。H3/H5 聚焦回归为 88 passed in 0.41s，包含 17 项混合 Loop 和 11 项预算测试。最终完整回归为 **266 passed in 3.32s**，实际命令如下；占位凭据只用于离线 import/stub，测试没有真实模型请求。

```bash
cd /Users/ouyangkun/PHD/Project/Fairy-Jev-hybrid
DEEPSEEK_API_KEY=offline-regression-placeholder DASHSCOPE_API_KEY=offline-regression-placeholder \
  .venv/bin/python -m pytest -q tests/jev tests/test_replay_contract.py tests/test_fairy_migration_smoke.py \
  > runs/agent_comparison/checkpoints/H5_release_regression.txt
```

| 检查点 | 实际状态 | 结果与命令／文件／证据记录 |
|---|---|---|
| H0 | passed | [基线冻结、165 项回归、31 个源文件哈希](../runs/agent_comparison/checkpoints/H0/checkpoint.json) |
| H1 | passed | [已有 LLM provider、CLI 和 updater 离线接口](../runs/agent_comparison/checkpoints/H1/checkpoint.json) |
| H2 | passed | [packet 替换、真实 SDK 序列化大小、证据前缀](../runs/agent_comparison/checkpoints/H2/checkpoint.json) |
| H3 | passed | [K=4 调度、错误和硬预算](../runs/agent_comparison/checkpoints/H3/checkpoint.json) |
| H4 | passed | [原生执行适配、trace 及回放审核](../runs/agent_comparison/checkpoints/H4/checkpoint.json) |
| H5 | passed | [266 项回归、冻结 R 的三组真实环境 scripted](../runs/agent_comparison/checkpoints/H5/checkpoint.json) |
| H6 | blocked | [1 次实际 Jev HTTP 451，另两组缺已有 LLM 凭据](../runs/agent_comparison/checkpoints/H6/checkpoint.json) |
| H7 | blocked | [九个正式槽位均保存阻塞证据；0/9 实际执行](../runs/agent_comparison/checkpoints/H7/checkpoint.json) |
| H8 | passed | [实现、有限实验阻塞报告、标签复现与版本交付](../runs/agent_comparison/checkpoints/H8/checkpoint.json) |

冻结提交上的 [offline 结果](../runs/agent_comparison/hn58-three-agents-v1/offline_frozen/offline_index.json) 为：三组各 10 次真实模拟工具执行，五项独立断言和原生 validator 全部通过，`scripted_task_success=true`，trace 与动作 replay 通过，HTTP 为 0。另从实际实验标签创建 `/Users/ouyangkun/PHD/Project/Fairy-Jev-hn58-reproduce`，使用新建、按锁文件安装的虚拟环境；模块路径指向该新目录，SDK 为 0.7.5，三组 offline 再次全部通过。该复现目录不用于另起 live 额度。源工作树的 31 个白名单文件与 H0 哈希仍逐项一致，见 [保留检查](../runs/agent_comparison/checkpoints/H8/source_preservation.json)。

正式逐次结果见 [formal_index.json](../runs/agent_comparison/hn58-three-agents-v1/formal_index.json)、[自动汇总](../runs/agent_comparison/hn58-three-agents-v1/comparison_report.json) 和带具体 blockers／回放状态的 [交付索引](experiments/hn58_three_agents_v1_results.json)。下表每个格子代表一个预定独立槽位，按表内顺序记录；没有将未运行样本计为任务失败。

| 区块／seed | 第一个槽位 | 第二个槽位 | 第三个槽位 |
|---|---|---|---|
| 1／1904 | llm_native：blocked，缺 DeepSeek key | jev：blocked，既有 HTTP 451 | hybrid：blocked，缺 key 且 Jev 访问受限 |
| 2／1905 | jev：blocked，既有 HTTP 451 | hybrid：blocked，缺 key 且 Jev 访问受限 | llm_native：blocked，缺 DeepSeek key |
| 3／1906 | hybrid：blocked，缺 key 且 Jev 访问受限 | llm_native：blocked，缺 DeepSeek key | jev：blocked，既有 HTTP 451 |

三组各计划 3 次，实际执行 0 次、阻塞 3 次；九条记录的 `task_success`、`live_integration_success`、原生 validator 均为 null，五项任务断言未评估，HTTP 和工具次数均为 0。动作回放为 `not_applicable`，trace 校验通过只说明阻塞记录完整。运行目录仍保留预检墙钟时间；它不是完成任务的延迟，不进入架构中位数或范围统计。所有 live 记录都没有 token usage，tokens 为 null、费用为 unavailable。全任务消耗以持久账本为准：**1 episode、1 HTTP、0 工具**，不能用正式汇总的零请求替代这笔 smoke 消耗。

目前没有真实混合动作片段，也没有有效的正式任务样本，因此无法判断混合方案是否更快、更省或更可靠，不能据此选优。下一步先补齐已配置的 DeepSeek 凭据，并由 provider 解决 Typesafe 的受限访问；无需改用新付费模型或调整 K。现有门禁保存了 smoke 阻塞证据，不会因再次执行 formal 就自动忽略它。解除外部条件后，应保留原记录，明确审核恢复依据／新实验身份，并沿用同一持久剩余额度（最多 11 个 episode、1,439 次 HTTP）；不重置已用的 Jev smoke 槽位。现阶段没有证据支持扩大 K 搜索、异步规划或场景范围。
