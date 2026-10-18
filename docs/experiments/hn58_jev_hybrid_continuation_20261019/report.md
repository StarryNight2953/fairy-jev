# HN58：纯 Jev 与 LLM + Jev 续执行补充实验

两种架构各执行一次真实完整运行。**两组都实际等待并完成灌后复查，未复现原生 Qwen“灌溉后只描述未来计划、没有执行等待复查”的具体行为。** 两组仍未通过完整任务验收：纯 Jev 缺少灌溉前目标区状态证据；Hybrid 也缺少该证据，并且复查后直接 FINISH，没有报告。Hybrid 另出现可直接定位的材料事实错误：Qwen 将真实配额 **1.4** 写成“无限额”，该错误原样进入 Jev 的真实决策输入。

本报告区分实际工具行为、既有验收判定与原因推断。没有修复 Agent，没有替换模型动作，没有追加补测。

## 运行结果与原生对照

| 指标 | 原生 Qwen（历史 v2 样本） | 纯 Jev（本轮） | LLM + Jev（本轮） |
| --- | --- | --- | --- |
| `live_integration_success` | true | true | true |
| `task_success` | false | false | false |
| 灌前天气、土壤、目标状态、配额 | 通过 | 失败：缺目标状态 | 失败：缺目标状态 |
| 仅43–63垄、每垄0.8小时 | 通过 | 通过 | 通过 |
| 实际配额消耗 | 通过 | 通过 | 通过 |
| 等待响应后完整复查 | 失败 | 通过 | 通过 |
| 现有最终报告断言 | 失败 | 通过 | 失败 |
| 终止原因 | `agent_finished` | `model_finish` | `model_finish` |
| 真实模型 HTTP / 工具次数 | 3 / 5 | 17 / 10 | 12 / 7 |
| 完整 episode 墙钟秒 | 26.403 | 10.029 | 30.236 |
| 输入 / 输出 tokens | 35,772 / 645 | 218,115 / 9,812 | 139,003 / 7,614 |

`execution_status=completed` 仅表示 Loop 正常结束，两组任务结果均为 **failed**。两组都没有预算、超时、服务错误导致的强制停止。新增29次模型HTTP均200，无HTTP重试。原生列复用 `20261018T165324_llm_native_live_3b5f3748`，本轮没有新原生样本，冻结请求 A/B 诊断也不算完整任务运行。

旧原生有终端文本，但其中等待复查仍是未来计划，不满足现有事实报告复合断言；本轮Hybrid则确实没有通信工具调用。这两种情况分别记录。

纯 Jev：`20261018T173848_jev_live_a0df2d85`。Hybrid：`20261018T173915_hybrid_live_41d9cea1`。运行 ID 使用 UTC，批次目录使用北京时间；均属本地2026-10-19这次实验。

## 纯 Jev：继续完成了等待、复查和报告

以下 `E编号` 对应各自 `trace.jsonl` 的 `event_id`，文件行号为编号加1。

实际顺序：天气 E11 → 全土壤传感器 E21 → 库存与配额 E31 → 灌溉 E47 → `advance_time(seconds=7200)` E63 → 全土壤复查 E73 → 两次保守通信 E89/E105 → 目标区状态 E121 → 事实报告 E137 → 原始HTTP响应选 FINISH E142 → 解析 E143 → `model_finish` E145。

首次自动验收问题在 **E47 / step_0004**：执行灌溉前，没有取得现有 `target_state` 口径认可的目标区状态。事前天气、土壤和配额均已取得；剂量和范围正确。E121后补目标区状态不能补救“灌前核查”的时间顺序。

E73传感器实际返回覆盖43–63，并发生在返回的灌溉响应时间之后；E137真实发送了事实报告。因此不能因为 `task_success=false` 就称它复现原生的等待复查缺失。

E89/E105的报告候选仍称“尚未同时确认…完整目标区复查”。现有确定性候选报告模板更保守，随后范围状态查询 E121 才使 E137 模板确认完成；现有语义验收则已接受 E73 的完整传感器覆盖。这说明存在重复通信及模板/验收口径差异。报告文本来自原有候选生成器，不能当作 Jev 自由生成的推理，也没有在本轮修改。

## Hybrid：等待复查完成，但在报告前 FINISH

实际顺序：当前时间 E25 → 天气 E37 → 库存与配额 E49 → 全土壤 E61 → 灌溉 E92 → `wait_for_notification(timeout=7200)` E111 → 全土壤复查 E123 → 原始响应 FINISH E130 → 解析 E131 → `model_finish` E133。整个运行没有 `AgentUserInterface__send_message_to_user`。

这次等待确实有效：E111结束仿真时间 **1786067540.4302242**，已超过灌溉 `effect_ready_at=1786067539.671976`；E123开始于 **1786067540.9886773**，返回覆盖43–63。因此这里不能笼统把通知等待当成无效，也不能从 `result=null` 推断工具失败。

E129实际 FINISH 请求中保留原任务、packet_0002 和最新真实事件 `[92,111,123]`。Jev选择停止，Loop接受该选择，没有通信报告。因此，**缺少等待复查的具体故障没有重现，但“尚有验收要求未满足便结束”在报告阶段出现了。** 该结论针对现有报告合同，不能直接等同于违反公开任务中不存在的精确报告评分词汇。

首次自动验收问题仍更早：**E92 / step_0005** 灌溉前缺目标区状态。最终报告失败是另一项问题。

## Hybrid 配额材料事实错误：原始证据链

| 位置 | 直接观测 |
| --- | --- |
| E49 工具结果 | `irrigation_quota_mm_total=1.4`，`irrigation_used_mm_total=0.0` |
| E68 真实 Qwen HTTP 输入 | `fresh_events` 中 E49整条记录与原工具结果完全相等，未截断或失真 |
| E69 原始 Qwen HTTP 响应 | 写出 `irrigation quota null/unlimited` 及 `Quota is unlimited.` |
| E74 接收的 packet_0002 | 原样保留错误 brief，引用包含 E49 |
| E79 工具选择、E87 参数选择的真实HTTP输入 | `state.packet` 与 E74完全一致；此时 `fresh_events=[]`，错误材料进入 Jev |
| E92 实际灌溉反馈 | 再次返回真实配额1.4和已用1.3125，作为后续选择的新反馈 |

人工语义核查可以在 **E69** 定位明确事实矛盾，早于规则验收自动定位的 E92。二者分别保留在 `analysis/hybrid/quota_contradiction.json` 与 `comparison.json`，没有改写原始 evaluator 结果。

两组都在灌溉前实际选择了 `get_inventory`（各自第3个工具），且收到真实配额；不能再归类为“没有找到配额工具”。两组都未调用 irrigation log。Hybrid本轮的错误发生在理解/压缩已返回的配额证据时。真实剂量仍是1.3125场均毫米，低于1.4；因此没有发生超配额灌溉，不能据此断言错误摘要造成了越额，也不能确认它导致了 FINISH。

初始Hybrid材料 E14曾建议边界43垄的额外核查，更新材料 E74将其弱化为“if possible”，但实际没有执行目标状态查询。这是可见建议和动作记录；不揭示模型内部为何选择跳过。

## 终止机制与能够下的结论

原生代码 `fairy/agents/agent/agent.py:386` 遇到空 `tool_calls` 就 `agent_finished`。Jev代码 `fairy/agents/Jev/agent.py:196` 遇到真实 Choice 的 `FINISH` 就 `model_finish`；Hybrid继承这条 Loop（`hybrid/agent.py:10`）。两者都没有在这一终止分支调用独立任务验收。选择 FINISH 的模型是 Jev；Hybrid里的 Qwen只写建议材料，不能直接执行工具或结束 Loop。

Hybrid按原有K=4在0和4个工具之后更新两次，7个工具后终止，没有第三次更新。这符合原有调度规则，不是Updater漏调用的证据。新的灌溉、等待和复查反馈已作为fresh事件传入最后的Jev请求。

- **直接证据**：两组均真正等待、复查；均缺灌前目标状态；纯Jev报告通过，Hybrid无报告；错误配额材料从Qwen原始响应进入Jev。
- **有支持的推断**：显式FINISH没有自动保证整个任务完成；本次Hybrid在报告阶段暴露了这一限制。原生与Jev的具体停止触发方式不同。
- **可能原因**：模型可能将传感器覆盖视为足够的“目标状态”，或者将灌溉和复查完成视为结束条件。这里只是解释候选，不是模型内部原因的证据。
- **无法确认**：错误摘要对最终动作的因果贡献、K的因果贡献、失败频率、架构总体优劣，以及加上冻结请求B规则后是否改善。每组只有一次，没有远端采样seed；场景seed1904不能保证远端确定性。不得以更短延迟证明效率提升。

公开任务要求“目标区状态”，但没有指定 `growth_stage` / `irrigation_pending` 等代理字段；当前验收使用这些字段认定目标状态覆盖。公开任务也没有列出完整报告评分词汇。这里保留原验收口径，同时将实际行为单独列出，不将代理断言等同于普适语义真值。

## 请求用量与旧结果

| 本轮角色 | HTTP次数 | HTTP合计延迟秒 | 输入tokens | 输出tokens |
| --- | ---: | ---: | ---: | ---: |
| Jev 工具Choice | 11 | 7.296 | 182,533 | 9,422 |
| Jev 参数Choice | 6 | 2.082 | 35,582 | 390 |
| Hybrid Jev工具Choice | 8 | 5.416 | 112,436 | 6,843 |
| Hybrid Jev参数Choice | 2 | 0.585 | 4,710 | 207 |
| Hybrid Qwen updater | 2 | 23.398 | 21,857 | 564 |

实际响应模型分别为 `jev-1.13.0` 与 `Qwen3.6-35B-A3B-FP8`，SDK为TypeSafe0.7.5/OpenAI2.44.0。tokens为服务报告值，跨模型的统计口径未额外校准；费用没有可靠计价来源，记录unknown。原始概率、confidence、usage、请求ID、HTTP延迟及完整请求响应均保留，不把confidence解释为已校准的任务成功概率。

旧Hybrid `20261018T140025_hybrid_live_9fd09d81` 五断言为 false/true/true/true/false：缺灌前目标状态及配额，无报告。本轮仍缺目标状态和报告，但已查到配额，另观察到摘要误读。旧Hybrid使用旧工具描述，本轮使用v2描述；不是重复同一个固定输入，不能从单例变化推断唯一因果。更早纯Jev MVP的实现和验收版本不同，本轮不将其作为严格配对样本。

## 冻结条件、授权与保全

实际执行提交 **`da7330ea625c675c4321083f062891e8061486c4`**，两次运行时工作树干净。两组配置从此前v2预登记逐字节复制；同一HN58场景、seed1904、checkpoint hash `808b2f7ff5cd2351eeabed1a9e9aa58611d96a8e5c0c40322129c458aa2cda7d`、初始时间1786057131.7453332、58工具+FINISH及新inventory描述。真实工具Choice和两个Updater请求都验证了描述传播。`identity_comparison.json` 核对了与旧原生样本共同的场景、时钟、catalog、参数候选、Prompt、evaluator、依赖和数据身份。

与旧原生真实提交bbe762c相比，生产范围仅有先前已经提交的access.py历史451成功证据替代规则变化。本轮没有修改Prompt、候选、K、Loop、工具、验收或模型参数，也没有把冻结请求B组的续执行规则带进来。使用现有代理策略，本地SDK选择的代理为127.0.0.1:7897；未切换出口或进行额外模型探测。

按用户本轮明确授权“这一次测试别管你的预算账本约束，我就要结果”，累计槽位从12调整为刚好容纳两组的13；同一ledger ID `ba456f29add04993a04ac60636f1dc0f`、所有历史行和HTTP计数继续保留，未重置。新增 **2个episode、29次HTTP**，最终累计 **13个槽位、66次HTTP**；单次40工具/40轮/120HTTP/600秒上限不变。授权及改动前后快照独立归档，预登记中的pending字段由 `budget_authorization.json` 解决，原预登记未事后改写。

离线回归 **302项通过**，网络连接被禁止。两份trace完整性校验、动作时间回放均通过，回放0模型请求并复现各自任务失败；验证通过不意味着任务通过。1,675个原有文件及1个原有缺失路径保全检查通过，另两个工作树HEAD/status及旧标签保持原样。

## 证据入口

- `comparison.json`：逐组断言、实际行为、首次问题、用量和旧对照。
- `live/*/`：原始manifest、trace、workflow、evaluation、metrics、catalog、参数支持表、verification和replay。
- `analysis/{jev,hybrid}/model_calls.jsonl`：从trace原样提取的每个真实请求及原始响应。
- `analysis/{jev,hybrid}/tool_timeline.json`、`finish_window.json`、`irrigation_window.json`：调用顺序与关键窗口。
- `analysis/hybrid/quota_contradiction_window.json`：工具事实→Qwen输入→错误响应→Jev输入完整窗口。
- `old_controls/`：旧原生v2及旧Hybrid样本副本，原件未修改。
- `source_code/`、`source_identity.json`：运行提交的相关源码及哈希。
- `preregistration.json`、`budget_authorization.json`、`budget_*.json`、`commands.json`：运行计划、授权、计账和实际命令。
- `evidence_index.json`、`files.sha256`：交付定位与文件校验。

完整证据批次目录：`runs/agent_comparison/hn58_jev_hybrid_continuation_20261019T013628+0800/`。同名 `.tar.gz` 为完整证据包，旁边 `.sha256` 为包校验；版本化报告目录：`docs/experiments/hn58_jev_hybrid_continuation_20261019/`。
