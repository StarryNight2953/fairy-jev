# Qwen3.8全场景实验：终止后已有结果分析

实验已于 **2026-10-19 19:51:51（北京时间）终止**。调度器、3个worker、报告监视进程和防休眠进程已退出，并设置`restart_authorized=false`。738个计划槽位中：**318组已有终态运行结果、3组执行中被中断、417组未启动取消**。318包含服务错误和达到上限的记录，不代表318个任务完成；738条索引包含取消标记，不代表执行了738次实验。

这批结果已经足以定位多项工程问题，但**不足以比较三种架构的任务成功率，也不能把所有问题归因于evaluator**。本次仅终止进程、保全证据和离线分析，没有新模型调用、重跑或改变原始评分。

## 1. 实际完成了哪些观察

| 架构 | 已有终态记录 | L1 / L2 / L3 | 停止原因 | 中断 / 未启动 |
| --- | ---: | --- | --- | --- |
| 原生Qwen | 11 | 11 / 0 / 0 | 11次调用超时退出 | 1 / 234 |
| 纯Jev | 120 | 78 / 42 / 0 | 26次FINISH、31次300步上限、63次决策错误 | 1 / 125 |
| LLM + Jev | 187 | 78 / 78 / 31 | 全部初始Qwen updater超时 | 1 / 58 |

三架构共同留下终态记录的场景只有11个，全是L1；**没有一个场景三架构都进入真实工具执行**，因为所有Hybrid都在初始材料更新失败。调度按固定顺序运行，每种架构每场景一次，停止导致覆盖严重不均衡，不是随机抽样。

| 口径（仅318组终态记录） | 原生Qwen | 纯Jev | Hybrid |
| --- | --- | --- | --- |
| `live_integration_success` true / false | 0 / 11 | 57 / 63 | 0 / 187 |
| `task_success` true / false / unknown | 0 / 1 / 10 | 0 / 1 / 119 | 0 / 0 / 187 |
| 原生弱validator true / false / unknown | 7 / 4 / 0 | 36 / 84 / 0 | 0 / 0 / 187 |
| 实际HTTP次数 | 458 | 23,945 | 374 |
| 真实工具次数 | 205 | 12,655 | 0 |
| episode耗时中位秒 | 1,003.278 | 25.928 | 60.596 |

这里的57个Jev集成通过包含31个达到步数上限的样本；集成接通不等于任务完成。原生弱validator按现有`native_contract_success`或`native_validation.success`回退口径汇总；HN58的原始字段null未被改写。Hybrid短耗时来自快速失败，不能据此称其高效。

## 2. 已确认的五类问题

### A. Qwen调用配置使测试主要消耗在超时与重试

11组原生：458次HTTP中161次200、297次ReadTimeout；98.70%的episode墙钟花在HTTP等待，79.84%直接花在超时等待。每组最后都是同一请求重复18次，约548秒后退出：Agent最多6次调用乘SDK每次最多3次HTTP。11组这些尾部共100.49分钟。205次工具执行合计仅0.258秒。

187组Hybrid：374次updater HTTP全部ReadTimeout，0次accepted packet、0次Jev调用、0次工具执行。每组两个30秒超时。因此它们尚未提供Hybrid决策能力的实测样本。

成功的161次原生响应全部`finish_reason=tool_calls`，输出17–136 tokens；33次附带非空assistant文本，但无无工具的终端回答。这与非流式返回、30秒读取阈值可能截断较长输出的解释一致，但超时响应的实际内容未知，不能断言生成报告或服务端某种故障就是根因。之前19:03的服务快照可作为旁证，不能替代逐请求服务日志。本次没有再次请求服务。

详见 [Qwen逐请求统计](/Users/ouyangkun/PHD/Project/Fairy-Jev/llm-jev-hybrid/runs/agent_comparison/full_l1_l2_l3_qwen38_fresh_20261019T163336+0800/analysis/stopped_review/qwen_analysis.json) 和 [此前耗时诊断](/Users/ouyangkun/PHD/Project/Fairy-Jev/llm-jev-hybrid/runs/diagnostics/native_qwen_latency_20261019T190329+0800/report.md)。

### B. 15个场景传给模型的任务正文已经错误

**7个L1、8个L2场景**调用`build_management_l1_flow`或`build_management_l2_flow`时，把`briefing_text`与`prefix`的位置传反。实际任务变成`o_*`短标识，而完整中文任务落在前缀参数中。现有preflight检查接口、初始时钟等，没有检测此语义错误，故246场景仍全部preflight通过。

当前已有28条终态记录受影响：12条Jev、15条Hybrid、1条原生。计划总体涉及15场景×3架构=45槽位，不能把45误写成已执行样本数。

直接例子：

- 当前打开的 [R5灌溉场景源码](/Users/ouyangkun/PHD/Project/Fairy-Jev/llm-jev-hybrid/fairy/scenarios/farm_world_fullseason_v2/l2_l1_splits/hb_disease_then_drought_recovery_tradeoff/scenario_l1_hb_disease_drought_r5_irrigation_20_43_action.py:44) 把`'o_r5_irrigation_20_43'`放在第四参数；[helper签名](/Users/ouyangkun/PHD/Project/Fairy-Jev/llm-jev-hybrid/fairy/scenarios/farm_world_fullseason_v2/l2_l1_splits/_batch_generated_split_common.py:337) 的第四参数实际是`briefing_text`。
- L2杀虫场景 [真实trace E2与E5](/Users/ouyangkun/PHD/Project/Fairy-Jev/llm-jev-hybrid/runs/agent_comparison/full_l1_l2_l3_qwen38_fresh_20261019T163336+0800/live/20261019T112343_jev_live_3a799d35/trace.jsonl:3) 的任务及HTTP `state.task`都是`o_r5_insecticide_36_55`。

这是任务构造缺陷，不是模型不遵循完整任务的证据，也不是evaluator能补救的问题。完整15场景、源码位置、受影响运行和实际输入见 [任务输入审计](/Users/ouyangkun/PHD/Project/Fairy-Jev/llm-jev-hybrid/runs/agent_comparison/full_l1_l2_l3_qwen38_fresh_20261019T163336+0800/analysis/stopped_review/malformed_task_scenarios.json)。HN58不在这15个场景之内。

### C. 纯Jev真实触发上下文上限

120组纯Jev中的63次决策错误，**62次为HTTP400 `max_tokens_exceeded`**，另1次为服务断连。上下文超限中位发生在第30轮。例 [E342原始服务响应](/Users/ouyangkun/PHD/Project/Fairy-Jev/llm-jev-hybrid/runs/agent_comparison/full_l1_l2_l3_qwen38_fresh_20261019T163336+0800/live/20261019T083659_jev_live_d254aa4d/trace.jsonl:343)：`{"detail":{"error_type":"max_tokens_exceeded"}}`。

这说明当前上下文路径确实会提交超过服务限制的请求；不能因为客户端进行了事件整理，或候选数量有限，就认为输入上限已经得到保证。超限是直接的服务响应，独立于任务评分器。62/120描述本批已观察记录，不是所有246场景或架构总体故障率。

### D. 候选覆盖和报告模板在跨场景时不充分

**缺失正确剂量**：`20261019T083802_jev_live_fb172a10`的E2任务要求杀菌剂3.8 L/垄，E97所有候选却只有2.0或4.0。模型在E105/E153/E201选择4.0，24垄共96 L。它无法从当时提供的菜单选出3.8。见 [候选 E97](/Users/ouyangkun/PHD/Project/Fairy-Jev/llm-jev-hybrid/runs/agent_comparison/full_l1_l2_l3_qwen38_fresh_20261019T163336+0800/live/20261019T083802_jev_live_fb172a10/trace.jsonl:98)。候选生成状态为ready仅证明生成器正常返回，不证明正确动作可表达。

**灌溉报告模板用于收获任务**：收获8–23垄样本`20261019T090354_jev_live_3ce5d1f7`要求产量、入库和风险报告，E213通信候选只有一条灌溉模板。E221至E4765、step16–300连续285次发送完全相同的“成功启动灌溉0次…”文本，然后达到步数上限。见 [候选 E213](/Users/ouyangkun/PHD/Project/Fairy-Jev/llm-jev-hybrid/runs/agent_comparison/full_l1_l2_l3_qwen38_fresh_20261019T163336+0800/live/20261019T090354_jev_live_3ce5d1f7/trace.jsonl:214) 和 [第一个重复报告 E221](/Users/ouyangkun/PHD/Project/Fairy-Jev/llm-jev-hybrid/runs/agent_comparison/full_l1_l2_l3_qwen38_fresh_20261019T163336+0800/live/20261019T090354_jev_live_3ce5d1f7/trace.jsonl:222)。这既有菜单能力缺口，也有实际重复选择行为；尚未通过对照确定二者的因果贡献。

31个步数耗尽样本中，23个发送至少200条通信。纯Jev总计12,655次工具调用中，7,264次为通信，占57.4%。不能把这些调用都理解为农场管理进展。

同时，26次FINISH中25次没有通信，但这些25份已保存任务没有明确“报告/汇报/report”字样，不能统一据此判为报告失败。已确认问题与自动词语筛查待复核项分开保存在 [Jev分析](/Users/ouyangkun/PHD/Project/Fairy-Jev/llm-jev-hybrid/runs/agent_comparison/full_l1_l2_l3_qwen38_fresh_20261019T163336+0800/analysis/stopped_review/jev_analysis.json)。

### E. 评价体系与回放器自身也有问题，但不是所有失败的解释

**语义验收覆盖不足**：本批只有HN58使用独立`hn58_semantic_v2`合同。其他245场景使用`scenario_native_v1`，明确`observational_only`、`task_success=None`。L1/L2原生validator主要检查工具error或仓储warning；90个L3没有独立validator。于是318条终态记录中仅2条严格task_success=false，另外316条unknown，不能输出“成功率0%”。见 [评价代码](/Users/ouyangkun/PHD/Project/Fairy-Jev/llm-jev-hybrid/fairy/experiments/scenario_evaluation.py:101)。

原生弱validator的true通常不证明目标达成；false也可能只是曾发生过可恢复错误。例如整地样本一次attach失败，随后detach、attach、施肥和起垄成功，但原生false仍保留。轨迹相似度和evaluator的first_issue也分别只是路径指标、最早问题事件，不能直接作为最终失败原因。

**4条原生回放false存在明确分类缺陷**：8条原始工具error在回放中逐条重现，返回结果完全相同，动作效果/配额/待处理灌溉比较也均通过。原生workflow仍将这些错误返回记为`op_type=TOOL`，而 [replay.py:72](/Users/ouyangkun/PHD/Project/Fairy-Jev/llm-jev-hybrid/fairy/agents/Jev/replay.py:72) 只用`TOOL_ERROR`认定源错误，于是在`:92`产生`source_error=false`与实际error不一致。4条原始false保留；不能把它们写成物理环境发散或新的模型失败。

全部318条终态trace完整性验证通过；Jev120条回放通过，原生7条通过、4条上述分类不一致，Hybrid187条无工具故未回放。完整等值证明和源码位置见 [评价与回放审计](/Users/ouyangkun/PHD/Project/Fairy-Jev/llm-jev-hybrid/runs/agent_comparison/full_l1_l2_l3_qwen38_fresh_20261019T163336+0800/analysis/stopped_review/evaluation_audit.json)。

## 3. 本轮HN58逐项复核

| 严格断言 | 原生Qwen3.8 | 纯Jev | Hybrid |
| --- | --- | --- | --- |
| 灌前天气/土壤/目标状态/配额 | false | false | 未形成任务结果 |
| 指定43–63垄、每垄0.8小时 | true | true | 未执行 |
| 配额消耗正确 | true | true | 未执行 |
| 等待后完整复查 | false | true | 未执行 |
| 现有最终事实报告断言 | false | true | 未执行 |
| `live_integration_success` | false | true | false |
| `task_success` | false | false | unknown |

纯Jev仍只缺现有验收口径下的灌前`target_state`，剂量、配额、等待、完整复查和报告通过。因此，该单例是否应因“目标状态”代理字段缺失而整体判失败，仍值得按公开任务语义复核；本次没有放宽断言或改写结果。

原生Qwen本轮**确实等待2小时**，且在ETA后读取S6。但S6只覆盖54–63垄，遗漏43–53；灌前土壤覆盖与灌后完整复查均不足。它随后因HTTP超时退出，不是成功返回未来计划文本后主动停止。因此不能沿用旧Qwen3.6样本的“没有等待”结论，也不能说本轮原生已完成全目标区复查。此前耗时诊断中“执行了复查”仅指实际发生传感器读取，本次明确补充其覆盖不完整。

Hybrid连初始材料都未生成，无法用于判断HN58决策或终止语义。三组详情保存在 [summary.json的hn58字段](/Users/ouyangkun/PHD/Project/Fairy-Jev/llm-jev-hybrid/runs/agent_comparison/full_l1_l2_l3_qwen38_fresh_20261019T163336+0800/analysis/stopped_review/summary.json) 和 [评价审计](/Users/ouyangkun/PHD/Project/Fairy-Jev/llm-jev-hybrid/runs/agent_comparison/full_l1_l2_l3_qwen38_fresh_20261019T163336+0800/analysis/stopped_review/evaluation_audit.json)。

## 4. 应当先解决哪些问题

1. 先修任务构造参数错序，并让离线preflight核对实际送入模型的任务正文。坏输入样本保留，不能事后修输入后仍当同一次运行。
2. 独立诊断Qwen冻结请求的真实完成时长，统一可解释的超时/重试策略。现有187条Hybrid结果都被初始服务失败截断，继续批量提交不会补足其任务行为证据。
3. 针对当前Jev原始400响应完善上下文容量控制；同时检查正确剂量/范围是否能由候选表达，消息候选是否与场景任务匹配。
4. 修复回放错误类型分类；定义各任务独立的结果合同，明确允许的恢复过程、观测覆盖与报告义务，再讨论任务成功率。
5. 以上应分别版本化，以少量有代表性的冻结输入/离线检查验证。是否启动新的live实验需另行明确；本批已终止，不会自动补测。

这些是依据已保存证据的修复顺序建议，本次没有实施代码修复。62次上下文超限、缺少3.8候选、错误任务文本和回放分类误判均有直接证据；模型内部重复行为原因、Qwen服务端具体瓶颈、架构总体优劣仍无法确定。

## 5. 保全、版本与用量

执行版本保持 **`366fc8320d78afac002f996c66003f14854d7ca2`**。停止时已有5,264个原始文件（live、已有results、config、preflight）被哈希保全；后续只增加取消/中断索引并更新派生报告，没有伪造中断运行的最终workflow/evaluation。原场景、Agent、候选、Prompt、K、验收和运行trace均未修改。

本批321个实际启动episode共 **25,251次HTTP尝试**：终态318组24,777次，3个中断样本474次。仍用同一账本ID `ba456f29add04993a04ac60636f1dc0f`，累计HTTP为30,852；停止没有重置预算或增加请求，只将3个正在运行的账本状态改为`interrupted_not_retried`。

服务已报告的tokens：原生输入2,970,911/输出9,017；Jev输入499,063,132/输出11,269,161。Hybrid没有成功usage，实际消耗unknown；超时请求的服务端计算量也unknown，不能视作0。统计使用各服务原报告口径，没有跨模型校准或可靠费用换算。

本地所有该批进程已退出，不再发起请求；已在远端处理中的请求是否即时取消，客户端无法确认。318组的耗时包含并发重叠，不能相加当作批次实际经过时间。

## 6. 复核入口

- [停机状态与计数](/Users/ouyangkun/PHD/Project/Fairy-Jev/llm-jev-hybrid/runs/agent_comparison/full_l1_l2_l3_qwen38_fresh_20261019T163336+0800/termination_summary.json)、[进程停止证据](/Users/ouyangkun/PHD/Project/Fairy-Jev/llm-jev-hybrid/runs/agent_comparison/full_l1_l2_l3_qwen38_fresh_20261019T163336+0800/termination/process_stop_result.json)、[最终progress](/Users/ouyangkun/PHD/Project/Fairy-Jev/llm-jev-hybrid/runs/agent_comparison/full_l1_l2_l3_qwen38_fresh_20261019T163336+0800/progress.json)。
- [318组分析汇总](/Users/ouyangkun/PHD/Project/Fairy-Jev/llm-jev-hybrid/runs/agent_comparison/full_l1_l2_l3_qwen38_fresh_20261019T163336+0800/analysis/stopped_review/summary.json)、[完整738槽位索引](/Users/ouyangkun/PHD/Project/Fairy-Jev/llm-jev-hybrid/runs/agent_comparison/full_l1_l2_l3_qwen38_fresh_20261019T163336+0800/slot_index.csv)、[原始结果入口](/Users/ouyangkun/PHD/Project/Fairy-Jev/llm-jev-hybrid/runs/agent_comparison/full_l1_l2_l3_qwen38_fresh_20261019T163336+0800/evidence_index.json)。
- [Qwen逐请求统计](/Users/ouyangkun/PHD/Project/Fairy-Jev/llm-jev-hybrid/runs/agent_comparison/full_l1_l2_l3_qwen38_fresh_20261019T163336+0800/analysis/stopped_review/qwen_analysis.json)、[Jev逐组问题与窗口](/Users/ouyangkun/PHD/Project/Fairy-Jev/llm-jev-hybrid/runs/agent_comparison/full_l1_l2_l3_qwen38_fresh_20261019T163336+0800/analysis/stopped_review/jev_analysis.json)、[任务与评价审计](/Users/ouyangkun/PHD/Project/Fairy-Jev/llm-jev-hybrid/runs/agent_comparison/full_l1_l2_l3_qwen38_fresh_20261019T163336+0800/analysis/stopped_review/evaluation_audit.json)。
- [15个错误任务场景完整名单](/Users/ouyangkun/PHD/Project/Fairy-Jev/llm-jev-hybrid/runs/agent_comparison/full_l1_l2_l3_qwen38_fresh_20261019T163336+0800/analysis/stopped_review/malformed_task_scenarios.json)、[318组行为明细](/Users/ouyangkun/PHD/Project/Fairy-Jev/llm-jev-hybrid/runs/agent_comparison/full_l1_l2_l3_qwen38_fresh_20261019T163336+0800/analysis/behavior_review.json)。
- [停止前原始文件哈希](/Users/ouyangkun/PHD/Project/Fairy-Jev/llm-jev-hybrid/runs/agent_comparison/full_l1_l2_l3_qwen38_fresh_20261019T163336+0800/termination/preserved_before_finalization.json)、`validation.json`与`files.sha256`（本次交付核验）。
- 原始请求、响应、参数候选、工具与环境反馈继续保存在`../../live/*/trace.jsonl`，未改写或删除。事件E编号对应trace第E+1行。
