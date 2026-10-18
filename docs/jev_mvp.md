# FAIRY Jev MVP

目标场景是 `scenario_l1_three_cultivar_hn58_action_ready_irrigation`。独立实现位于 [`fairy/agents/Jev/`](../fairy/agents/Jev/)，入口为 `python -m fairy.agents.Jev`，配置为 [`configs/jev/hn58_irrigation.json`](../configs/jev/hn58_irrigation.json)。JevAgent 自己驱动串行 Loop，复用 FAIRY AppTool、Engine、TimeManager 和 Workflow。

本轮交付结果：环境和 scripted 闭环通过；最终 `live_integration_success=true`，`task_success=false`；真实运行及回放均保留了模型跳过灌溉前配额观察和灌溉后完整复查的证据。汇总结果见 [delivery_report.json](../runs/jev/delivery_report.json)，P0–P7 的实际命令、修改文件和证据在下方检查点表中。

本地基线提交是 `215f8c3b7afca66f24ccdcc007f81e0c7cbf4dc8`，Python 为 3.11.15。用户原有的 `pyproject.toml` 和 `uv.lock` 修改保留。基础依赖中的 `typesafe-sdk>=0.7.5` 保留，新增 `jev` extra 将本轮兼容版本固定为 `typesafe-sdk==0.7.5`。基线、实际源码审计和运行证据位于 `runs/jev/`；该目录沿用仓库已有忽略规则。

## 安装与运行

在仓库根目录执行。本机已有 `.venv` 使用 uv 管理，里面未预装 pip；本轮实际安装及依赖检查命令为：

```bash
/Users/ouyangkun/tools/miniconda/install_dir/miniconda3/envs/are/bin/uv sync --extra jev
/Users/ouyangkun/tools/miniconda/install_dir/miniconda3/envs/are/bin/uv pip check --python .venv/bin/python
.venv/bin/python -c 'import fairy, typesafe_sdk; print(typesafe_sdk.__version__)'
```

在已有 pip 的新项目环境中，也可使用标准安装契约：

```bash
python -m pip install -e '.[jev]'
```

配置中的 `api_key_env` 默认为 `TYPESAFE_API_KEY`。密钥只从环境读取。若本地 `env.sh` 已保存用户提供的变量，可在运行 live 或 sdk-smoke 前导出；命令不输出其内容：

```bash
set -a
source ./env.sh
set +a
```

工具审计和 scripted 模式不需要密钥。缺少密钥时，live/sdk-smoke 保存 `missing_api_key` 结果，`live_integration_success` 与 `task_success` 保持 `null`，不妨碍其他模式。

```bash
.venv/bin/python -m fairy.agents.Jev --mode inventory --config configs/jev/hn58_irrigation.json --output-dir runs/jev/inventory
.venv/bin/python -m fairy.agents.Jev --mode sdk-smoke --config configs/jev/hn58_irrigation.json --output-dir runs/jev/sdk_smoke
.venv/bin/python -m fairy.agents.Jev --mode scripted --config configs/jev/hn58_irrigation.json --output-dir runs/jev/scripted
.venv/bin/python -m fairy.agents.Jev --mode live --config configs/jev/hn58_irrigation.json --output-dir runs/jev/live
```

每次命令在指定父目录下创建带模式、时间和随机后缀的唯一 run 目录。终端输出实际 `output_dir`。回放时使用该目录中 `workflow.json` 的绝对路径，例如本次最终 live 运行：

```bash
.venv/bin/python -m fairy.agents.Jev --mode replay --config configs/jev/hn58_irrigation.json --workflow /Users/ouyangkun/PHD/Project/Fairy-Jev/runs/jev/live/20261017T112501_live_db897d9a/workflow.json --output-dir runs/jev/replay
```

其他运行将 `--workflow` 换成对应 run 的文件。回放不请求模型，默认从 workflow 同目录读取 `manifest.json`。源 manifest 的 scenario、seed、初始化参数、时钟策略、实际起点和 checkpoint hash 为权威来源；冲突配置会报错。

已保存运行可用独立核验命令离线检查，自动写入新的 `verification_<id>.json`，不覆盖原始证据：

```bash
.venv/bin/python -m fairy.agents.Jev.verify runs/jev/live/20261017T112501_live_db897d9a
```

## 两阶段决策与原生执行

| 模块 | 职责 |
| --- | --- |
| `client.py` | 官方同步 `TypeSafeClient`/`Choice` 后端与可注入 scripted 后端；保留请求模型、响应模型和响应元数据 |
| `tools.py` | 复用 `build_toolset` 和 FARM 隐藏规则；完整目录、真实签名校验、一次原生执行 |
| `candidates.py` | 只为已选工具构造完整实参组合；记录取值来源、单位与生成状态 |
| `state.py` | Agent 可见任务、观察、执行事实、控制器反馈及历史保留说明 |
| `agent.py` | 独立串行 Loop、预算边界、两次决策与一次工具执行的关联 |
| `trace.py` | 逐事件 JSONL 追加、及时 flush、密钥及 Authorization 脱敏 |
| `environment.py` / `environment_check.py` | 目标 fixture、显式 checkpoint 时钟、真实工具环境控制实验 |
| `evaluation.py` | 运行后的独立任务断言及原生 validator 原始结果 |
| `replay.py` | 复用已有 FAIRY 回放的时序与执行前错误处理契约 |
| `verify.py` | 离线核验已保存请求、候选、菜单、关联 ID、原生实参、Workflow 和终止计数 |
| `inventory.py` / `runner.py` | 源码与全场景审计、命令入口、配置和运行产物 |

每轮先向 Choice 提交完整工具目录。目标场景有 59 个注册工具，沿用 `FARM_AGENT_HIDDEN_TOOL_NAMES` 后可见 58 个，保留 `AgentUserInterface__send_message_to_user`。另加 `FINISH`，一级菜单恒为 59 个选项。选择工具后才调用参数生成器；无参数工具直接绑定 `{}`，其余工具即使只有一组候选也执行第二次 Choice。模型选择原样执行，候选来源字段不会进入原生 kwargs。

58 个可见工具分为 20 个完全无参数、34 个有必填参数、4 个仅可选参数。每项在 `parameter_support_matrix.json` 登记具体 resolver、必填/可选字段、取值来源与单位。本轮公开 briefing 和配置下，全部工具都能生成合法候选，最大为 192 项。它是有限离散目录；连续参数空间不作完整覆盖承诺。更改可见目标区或时长会改变候选，不能从 scenario ID、checkpoint 或 oracle 推导候选。

三个灌溉入口保持各自语义：`irrigate(start,end,hours)` 和 `irrigate_range(start,end,duration_hours)` 各执行一次区间调用，`irrigate_ridge(ridge_id,duration_hours)` 只执行一次单垄调用。单垄选择不会被改写为区间动作，也不会暗中展开为多次调用。

只有可选参数的四个工具有独立处理：`dry_grain` 保留真实默认 `target_moisture_pct=13.0`，`get_forecast` 保留 `days=7`；`advance_time` 总时长必须大于零，`wait_for_notification` 使用明确的正 timeout。等待候选来自可见 `effect_ready_at`/ETA 和声明的等待目录，不绑定 oracle 的固定等待时长。报告使用已执行动作及复查结果的确定性事实模板；覆盖不足、错误、未等待或未复查时不会声称完成整个区域。

缺少公开上下文时返回 `needs_context`，不执行动作，写入 controller feedback 后继续使用完整一级菜单。候选超过 255 时返回 `candidate_limit_exceeded`，保留候选证据并停止等待配置调整，不静默截断。类型校验区分 bool 和 int，拒绝未知参数、缺失必填字段与非有限数值。工具返回错误对象也记录为失败；工具执行不自动重试。

SDK 配置使用 `base_url=https://api.typesafe.ai`、`model=jev-1.13.0`、`timeout=30`、`retry=RetryPolicy(max_retries=0)`、`compression=False`。官方 SDK 参数名称是 `retry`，其 timeout 约束 HTTP 操作。当前默认上下文上限为每问题 32k、每请求 64k token，Choice 最多 255 项；本实现没有开启 long-context 请求头。以上契约于 2026-10-17 对照[官方同步客户端](https://docs.typesafe.ai/sdk/python/api/clients/sync)、[Choice](https://docs.typesafe.ai/primitives/choice)、[Models](https://docs.typesafe.ai/models) 与 [API reference](https://docs.typesafe.ai/api)核验。

## 可见状态、预算与完整记录

模型状态只包含可见 briefing、系统上下文、模拟时间、已返回的工具结果、环境通知、动作事实和 controller feedback。Engine 增加 `add_system_context` / `on_environment_event` 中立 hook；原有 Agent 继续走 messages 回退路径。通知的 Workflow 节点由选中的路径写入一次。briefing 提取跳过 oracle 事件。

默认观察上限 24、recent actions 上限 6、controller feedback 上限 12。状态保留每种工具的最新观察，按新旧顺序选取至观察上限；全部物理写操作和错误的动作事实保留在受运行预算约束的 `action_facts` 中，防止历史裁剪后漏算累计剂量。成功的用户报告不进入物理动作账本，其最新内容仍在 observations、最近 6 次动作仍在 recent actions，`report_history.sent_count` 保留报告次数。已经完整包含在 observations 的重复结果使用 `same_as_observation` 指向对应 `observation_id`，不重复传输同一结果。历史保留规则、遗漏的观察 ID 和反馈数量进入每次请求，完整原文仍在 trace。

默认运行预算为 40 steps、40 native tool calls、80 decision calls、600 秒墙钟、每次请求 HTTP timeout 30 秒、相同缺参失败上限 3 次。参数失败也消耗 step。预算在模型请求和工具执行边界检查，超限后不启动新请求或新动作；在途请求可能形成超时尾部，`run_end` 保存实际耗时及 `timeout_overrun_seconds`。600 秒配置不被当作强制中断在途 HTTP 请求的保证。

每次运行至少保存以下文件：

| 文件 | 可核查内容 |
| --- | --- |
| `manifest.json` | commit、SDK/model、模式、seed、scenario/init kwargs、配置、目录和 checkpoint hash、声明/实际起点与预算；新运行另存 `implementation_hashes` 与 `history_policy`，区分相同 commit 下的未提交实现 |
| `tool_catalog.json` | 完整描述、schema、真实默认值、签名、可见性、目录 hash 与顺序 |
| `parameter_support_matrix.json` | 全部原生工具的参数支持策略、来源和单位 |
| `trace.jsonl` | 完整模型 state/instructions/criteria、原始/解析响应、候选、执行结果、通知、错误与终止 |
| `workflow.json` | 可供已有 FAIRY 机制读取、含 `time_before`/`time_after` 的实际工具轨迹 |
| `evaluation.json` | 分离的集成覆盖、独立任务断言、native_validation 和运行后的物理证据 |
| `summary.md` | 本次运行结果和关键产物索引 |

Trace 的 `step_id`、`decision_id`、`attempt_id` 与 `tool_call_id` 分开计数。一轮动作关联一次工具 Choice 和可选的一次参数 Choice；一次工具调用只执行一次 AppTool。执行前参数错误有独立记录及唯一 ID，不增加实际执行计数。API 失败也保存已开始的请求和终止事件。服务未提供的 token、request ID 或 HTTP 字段保持 `null` 并注明原因；confidence 保存服务原值，没有返回的 reasoning 不作补写。

## 环境修正与独立验收

目标 fixture 原声明时间为 `2026-08-06 08:00:00+08:00`，checkpoint 的模拟时间与 physics last update 为 `1786057131.7453332`，即 `2026-08-07 06:58:51.745333+08:00`。原声明时间导致物理时钟领先，已在 [declared_clock_check.json](../runs/jev/checkpoints/P1/declared_clock_check.json) 保留失败证据。runner 的 `clock_source=checkpoint` 在创建 Engine 前调整本次 scenario 实例起点，保存原声明、有效时间与 checkpoint hash；checkpoint 和物理实现不改写。

Agent、Engine 及全部 App 使用同一个 TimeManager。独立环境控制实验执行 10 次真实工具调用，验证新增 action、配额消耗、等待后复查和土壤响应，模型请求数为 0。当前物理路径可能立即处理注水，验收允许这种实际语义，并要求按可见响应时间等待后复查。参见 [P1 checkpoint](../runs/jev/checkpoints/P1/checkpoint.json) 和[环境实验 evaluation](../runs/jev/checkpoints/P1/control_01/evaluation.json)。

独立任务验收只比较本次新增动作，在模型运行结束后才读取内部 action/physics 证据。检查：灌溉前完成天气、目标区土壤水分、目标区状态及配额观察；新增灌溉恰好覆盖 43–63，每垄累计 0.8 小时且无超量；实际配额变化符合公式且未超过总额度；动作已处理、有等待后全目标区复查及真实环境响应；复查后有事实报告。本地基准消耗为 `21 × 0.8 × 5 / 64 = 1.3125 field-mm`，总额度 1.4，配额容差 `2e-6 field-mm`、剂量容差 `1e-9 h`。

原生 validator 结果单独保留在 `native_validation`。仅返回 `started`、模型选择 `FINISH` 或原生 validator 成功都不能代替上述断言。空轨迹、错误区域、只灌一垄、重复超量、缺少关键观察或未完成复查均有反例测试。

`live_integration_success=true` 要求在同一完整证据链中出现真实工具 Choice、真实参数 Choice、原生执行，以及该结果进入后续真实 SDK 请求。`task_success=true` 要求环境有效并通过全部目标任务断言。提前 FINISH 或仅无参动作导致覆盖不足时，集成值为 `null`、`live_coverage_status=incomplete`，列出 `missing_stages`；已经真实运行且未完成任务时 `task_success=false`。实现接口、执行适配或轨迹错误使集成值为 `false`。未运行 live 或环境无效时，真实任务值保持 `null`。scripted 的任务结果单独使用 `scripted_task_success`。

回放复用 `scripts/rebatch_fos_from_traces.py` 的 recorded time 和执行前错误契约。执行前参数错误仅恢复错误事件，不调用工具。对比工具名、实参、新增 action、配额和关键返回字段；传感器随机值和网络耗时不要求逐字节一致。回放结果另存 `replay_task_success`，scripted 源运行不会由回放转换为真实 Jev 任务成功。

## 检查点和已保存结果

下表列出 P0–P7 的集中记录。P0–P6 均为 `passed`，P7 为 `completed_task_failed`：真实集成与回放检查完成，模型未完成严格任务。两次 live 的原始证据均保持原样。

| 检查点 | 当前状态与主要结果 | 证据 |
| --- | --- | --- |
| P0 基线、依赖、规模 | 通过。AST 统计 59 实现：apps 56、场景 1、A2A 2；普通运行名称并集 75，Agent 可见 74，单场景最多 64。初次 292 初始化通过、18 缺少 Excel；用户补充 workbook 后重新审计 310/310 通过 | [P0 checkpoint](../runs/jev/checkpoints/P0/checkpoint.json)、[初始审计](../runs/jev/checkpoints/P0/tool_audit.json)、[补齐数据后的审计](../runs/jev/checkpoints/P0/with_workbook/tool_audit.json)、[workbook 迁移](../runs/jev/checkpoints/P0/workbook_relocation.json) |
| P1 真实执行与时钟 | 通过。声明时钟问题复现；checkpoint 起点与共享 clock 通过；10 次真实控制调用，5 项任务断言通过 | [P1 checkpoint](../runs/jev/checkpoints/P1/checkpoint.json)、[控制实验 trace](../runs/jev/checkpoints/P1/control_01/trace.jsonl) |
| P2 官方 SDK | 通过。官方 MockTransport 验证序列化、元数据、错误与零自动重试；小型真实 smoke：1 次请求，无工具调用，它不代表闭环集成成功 | [P2 checkpoint](../runs/jev/checkpoints/P2/checkpoint.json)、[离线 SDK/Loop 检查](../runs/jev/checkpoints/P2/offline_sdk_and_loop_tests.json)、[真实 smoke](../runs/jev/sdk_smoke/20261017T111952_sdk-smoke_98955813/evaluation.json) |
| P3 全量目录与可见状态 | 通过。目标每轮 58+FINISH；oracle briefing 排除；历史裁剪、重复结果引用及物理动作账本经过验证 | [P3 checkpoint](../runs/jev/checkpoints/P3/checkpoint.json)、[工具目录](../runs/jev/checkpoints/P0/tool_catalog.json)、[状态回归](../tests/jev/test_state.py) |
| P4 参数目录 | 通过。58 行支持表，20/34/4 分类；全部策略可生成合法组合，最大 192，目标区和时长来自 briefing | [P4 checkpoint](../runs/jev/checkpoints/P4/checkpoint.json)、[支持表](../runs/jev/checkpoints/P4/parameter_support_matrix.json)、[生成结果](../runs/jev/checkpoints/P4/generation_results.json) |
| P5 独立 Loop/trace | 通过。两阶段决策、执行、反馈和异常记录可关联；最终 trace 离线核验 304 个事件，无问题 | [P5 checkpoint](../runs/jev/checkpoints/P5/checkpoint.json)、[Loop 测试](../tests/jev/test_loop.py)、[最终 trace 核验](../runs/jev/live/20261017T112501_live_db897d9a/verification_280d4264.json) |
| P6 scripted 闭环 | 通过。16 次 scripted decision、10 次原生工具调用、0 次模型请求、`scripted_task_success=true`；真实集成和真实任务均为 null。完整离线回归 165 passed | [P6 checkpoint](../runs/jev/checkpoints/P6/checkpoint.json)、[最新 scripted evaluation](../runs/jev/scripted/20261017T112434_scripted_06e52b94/evaluation.json)、[165 项回归](../runs/jev/checkpoints/P6/regression_final.txt) |
| P7 有界 live 与回放 | 完成，任务失败。首次 live 集成/任务均 false；唯一有限复测集成 true、任务 false，69 次请求、40 次原生执行后达到 max_steps。最终40次动作按记录时间回放无分歧，严格任务仍 false | [P7 checkpoint](../runs/jev/checkpoints/P7/checkpoint.json)、[首次 live](../runs/jev/live/20261017T112127_live_e58f8b9e/evaluation.json)、[第二次 live](../runs/jev/live/20261017T112501_live_db897d9a/evaluation.json)、[最终回放](../runs/jev/replay/20261017T112658_replay_5d11338d/evaluation.json) |

补充的 Tangyan5 workbook 保持原内容，位于 `fairy/scenarios/farm_world_fullseason/tangyan5_table_fitted/data/糖研所5号地数据汇总-1206_副本.xlsx`。场景默认路径已改为相对于源码文件的 package data 路径。初次审计的 18 个缺失数据失败保留，未追改为通过；新审计单独存放于 `P0/with_workbook/`。A2A 跨模式可见名称上界 87 是从注册入口推导的上界，本轮没有声称实测全部 A2A 配置。

首次真实运行是 `20261017T112127_live_e58f8b9e`：37 次 API 尝试，其中 36 次 HTTP 200、1 次 HTTP 400；21 次原生工具执行；实际墙钟耗时 23.732868 秒。环境有效，但 `live_integration_success=false`、`live_coverage_status=failed`、`task_success=false`、`stop_reason=decision_error`。完整两阶段执行反馈链已经覆盖，`missing_stages=[]`；集成失败来自末次请求的 `max_tokens_exceeded`，并非仅缺少覆盖。

首个有证据的任务错误发生在 `step_0005 / decision_0006`：模型在尚未观察剩余灌溉配额时选择 `FieldOpsApp__irrigate_range`，随后参数 Choice 选择了正确的 43–63 区间和 0.8 小时。第一次灌溉前观察断言因此已失败。之后确实等待了 7200 秒，但没有重新读取整个目标区状态；第 9–21 次工具调用反复发送报告。最终五项任务断言中，区域/累计剂量与配额消耗通过，灌溉前观察、等待后的完整复查和事实完成报告未通过。原生 `native_validation.success=true` 原样保留，没有用它覆盖严格任务失败。

末次请求暴露了本地状态实现问题：重复报告同时进入观察填充、最近动作和写操作历史，使输入增长至服务上下文上限。修正仅调整可见状态保留：每种工具只保留最新观察，recent actions 缩至 6，成功报告从物理动作账本排除但保留次数、最新内容和完整原始 trace。全部物理写操作、错误及相关引用继续保留；工具菜单、参数目录、提示、fixture 与任务断言均保持相同。新增 [test_state.py](../tests/jev/test_state.py) 验证从 10 到 40 次重复报告的请求体增长小于约 1 KB，并验证 40 次报告后物理灌溉和错误记录仍完整。单独运行该文件得到 5 passed。

唯一一次有限复测是 `20261017T112501_live_db897d9a`：69 次真实 API 请求全部 HTTP 200，40 次原生工具执行，墙钟耗时 42.790024 秒。`environment_check=passed`、`live_integration_success=true`、`live_coverage_status=complete`、`missing_stages=[]`，说明完整两阶段调用、原生执行和结果回流已验证。模型达到 `max_steps`，`task_success=false`，最早任务问题仍为第 5 步缺少灌溉前剩余配额观察。任务断言结果与首轮相同，原生 validator 仍返回 true；没有改变严格验收来消除失败。完整请求和原始结果见[第二次 trace](../runs/jev/live/20261017T112501_live_db897d9a/trace.jsonl)。

修正后最大服务报告输入为 25,426 token；首轮最大成功请求为 32,681 token，随后发生 HTTP 400。两次 live 加一次 smoke 合计 107 次真实 HTTP 请求：106 次 HTTP 200、1 次 HTTP 400；两次 live 原生工具执行共 61 次，排除 scripted、环境实验及回放。三次运行累计墙钟耗时 67.59806266595842 秒。

首轮真实轨迹在新 fixture 上完成 21 次原生动作回放，无分歧，`task_success=false` 得到保留。最终 live 回放 `20261017T112658_replay_5d11338d` 完成 40 次执行，40 步均使用 recorded time，新增 action、配额和 pending 状态比较全部一致，模型请求数 0、`task_success=false`。最新 scripted 回放 `20261017T112658_replay_3ae7a62e` 的 10 次 recorded-time 执行也通过，`scripted_task_success=true`，真实任务值保持 null；参见 [scripted replay evaluation](../runs/jev/replay/20261017T112658_replay_3ae7a62e/evaluation.json)。

独立 [verification_280d4264.json](../runs/jev/live/20261017T112501_live_db897d9a/verification_280d4264.json) 核验通过：304 个 trace 事件、69 次请求、40 次完整一级工具菜单（每次 59 项）、29 次参数 Choice、40 次原生执行。请求/响应终止、候选归属、选择实参、Workflow 和预算计数一致。对 186 个交付文件进行密钥原值扫描，匹配数为 0，扫描文件范围和结果见 [secret_scan.json](../runs/jev/checkpoints/P7/secret_scan.json)，报告不包含密钥原文。

此次有限复测只验证状态实现修正，首轮失败记录没有被替换。下一步最小实验是从可见历史提取简短的未完成事项，例如“尚未观察剩余配额”“响应时间后尚未复查目标区”，保留全量菜单、参数选择和当前任务断言。该建议尚未实施，也没有继续重复真实运行以寻找成功结果。

## 验证命令

完整离线回归使用占位环境变量满足已有 provider 模块在 import 时的配置检查，测试本身不访问这些模型服务：

```bash
DEEPSEEK_API_KEY=offline-regression-placeholder DASHSCOPE_API_KEY=offline-regression-placeholder .venv/bin/python -m pytest -q tests/jev tests/test_replay_contract.py tests/test_fairy_migration_smoke.py
```

本轮第一次扩大回归时，已有测试中的 3 项因缺少上述 provider import-time 环境变量失败；提供占位变量后通过，没有为此修改旧 provider 代码。补充验收反例和状态增长测试后的完整回归结果为 **165 passed in 2.74s**，见 [regression_final.txt](../runs/jev/checkpoints/P6/regression_final.txt)。live 之后仅改善 SDK HTTP 错误元数据的缺失原因说明，相关 SDK 回归 **12 passed in 0.10s**，没有改变模型动作或运行结果；计入 [delivery_report.json](../runs/jev/delivery_report.json) 的 validation 字段。

可单独检查官方 SDK、Engine 兼容与 Loop，以及重做真实环境控制实验：

```bash
.venv/bin/python -m pytest -q tests/jev/test_client.py tests/jev/test_engine_hooks.py tests/jev/test_loop.py
.venv/bin/python -m fairy.agents.Jev.environment_check --output-dir runs/jev/environment_control_repeat
```

环境控制实验与 scripted 都是开发验证证据，manifest 会标明对应 backend。它们的动作不作为真实 Jev 决策证据。旧 run 文件不会被覆盖；修复后生成新的 run 目录，并在最终检查点说明哪个结果对应当前实现。
