# 三种架构 × 全量 L1 / L2 / L3 单样本实验

**已按用户要求终止，非全量完成**。快照时间：2026-10-19T15:11:28.284663+08:00（北京时间）。
终止时已有 29 组保存完整运行结果，3 组在运行中被停止，706 组未启动。全部进程已停止，不会自动恢复。738条槽位记录包含取消标记，不代表完成738次实验。中断样本只有实际留下的证据，未伪造最终workflow或evaluation。

冻结提交：`ec00d248c95b31fe54a3148e2ccda946f04a5104`。预登记 246 个场景、738 个组合，每组合仅一次，场景 seed=1904；远端模型未配置采样 seed。
当前已保存 738 个槽位结果，其中有实际模型 HTTP 的结果 32 个；调度器快照显示活跃 0 个。未记录的 0 个槽位不计作失败。

## 如何解释验收结果

`task_success` 仅 HN58 灌溉场景沿用原有严格 semantic_v2 合同。其他场景没有此次新定义的独立语义验收，统一记录 unknown/null；L1/L2 的原生 validator 多数只检查工具错误，正式 L3 默认没有独立 validator。原生 true 不代表任务完成，原生 false 也可能只是可恢复的中间工具错误。现有 workflow 指标保留原数值，不设置新的成功阈值。
`live_integration_success` 衡量真实请求、工具执行及反馈接通，与任务完成分开。`execution_status=completed` 仅表示 Loop 正常结束；预算或时间停止为有界截断。unknown 不合并到 false。错误样本取自原评价器 `first_issue`，不能解释为全部语义问题的人工诊断。

## 分组执行与判定

表中真/假/未知均只统计已保存结果；执行指至少一次真实模型 HTTP。

| 层级 | 架构 | 计划/记录 | 执行 | 阻塞 | 正常结束 | 有界停止 | 集成 真/假/? | 任务 真/假/? |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| L1 | 原生 Qwen | 78/78 | 7 | 0 | 4 | 0 | 4/2/72 | 0/1/77 |
| L1 | 纯 Jev | 78/78 | 19 | 0 | 4 | 6 | 10/8/60 | 0/1/77 |
| L1 | LLM + Jev | 78/78 | 6 | 0 | 1 | 0 | 1/4/73 | 0/1/77 |
| L2 | 原生 Qwen | 78/78 | 0 | 0 | 0 | 0 | 0/0/78 | 0/0/78 |
| L2 | 纯 Jev | 78/78 | 0 | 0 | 0 | 0 | 0/0/78 | 0/0/78 |
| L2 | LLM + Jev | 78/78 | 0 | 0 | 0 | 0 | 0/0/78 | 0/0/78 |
| L3 | 原生 Qwen | 90/90 | 0 | 0 | 0 | 0 | 0/0/90 | 0/0/90 |
| L3 | 纯 Jev | 90/90 | 0 | 0 | 0 | 0 | 0/0/90 | 0/0/90 |
| L3 | LLM + Jev | 90/90 | 0 | 0 | 0 | 0 | 0/0/90 | 0/0/90 |

集成错误、上下文/候选停止、中断、原生判定和验证结果的完整分类见 `evidence_index.json.groups`；所有槽位见下方索引。

## 请求用量与延迟

仅合计服务已报告 tokens；缺失值保持未知，不能据此比较跨模型成本。延迟包含初始化、模型、工具和结束处理，使用全部有已记录墙钟值的结果，包括失败和有界停止。p95为最近秩法，单样本或小样本无稳定分位数含义。

| 层级 | 架构 | HTTP | 工具 | 输入 tokens | 输出 tokens | tokens记录数 | 墙钟n | 墙钟合计s | 中位s | p95s | 最大s |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| L1 | 原生 Qwen | 177 | 140 | 2,033,524 | 9,469 | 6 | 6 | 1960.584 | 139.843 | 835.346 | 835.346 |
| L1 | 纯 Jev | 4803 | 2460 | 90,954,269 | 1,826,813 | 18 | 18 | 2099.400 | 27.921 | 307.512 | 307.512 |
| L1 | LLM + Jev | 553 | 325 | 3,839,941 | 181,202 | 5 | 5 | 1528.882 | 343.986 | 539.265 | 539.265 |
| L2 | 原生 Qwen | 0 | 0 | 未知 | 未知 | 0 | 0 | — | — | — | — |
| L2 | 纯 Jev | 0 | 0 | 未知 | 未知 | 0 | 0 | — | — | — | — |
| L2 | LLM + Jev | 0 | 0 | 未知 | 未知 | 0 | 0 | — | — | — | — |
| L3 | 原生 Qwen | 0 | 0 | 未知 | 未知 | 0 | 0 | — | — | — | — |
| L3 | 纯 Jev | 0 | 0 | 未知 | 未知 | 0 | 0 | — | — | — | — |
| L3 | LLM + Jev | 0 | 0 | 未知 | 未知 | 0 | 0 | — | — | — | — |

已保存结果 HTTP 合计 **5533**；同一累计账本中本批次已预留 32 个 episode，含活跃运行的实时 HTTP 为 **5533**。累计账本从 66 增至 5599 次。进度快照中两种计数存在时间差；活跃请求不虚填为已结束结果。费用 unknown。

## HN58 原合同结果

以下仅列本批次的三个新样本，不覆盖旧实验；断言由原 evaluator 计算。

| 架构 | 状态 | 集成 | 任务 | 逐项断言 | 证据 |
|---|---|---|---|---|---|
| 原生 Qwen | completed | True | False | pre_irrigation_observations=False; new_irrigation_exact_target_and_dose=True; quota_consumption=True; processed_waited_rechecked_response=False; factual_final_report=False | [record](</Users/ouyangkun/PHD/Project/Fairy-Jev/llm-jev-hybrid/runs/agent_comparison/full_l1_l2_l3_once_20261019T142142+0800/results/scenario_l1_three_cultivar_hn58_action_ready_irrigation__llm_native__seed1904.json>) |
| 纯 Jev | completed | True | False | pre_irrigation_observations=False; new_irrigation_exact_target_and_dose=True; quota_consumption=True; processed_waited_rechecked_response=True; factual_final_report=True | [record](</Users/ouyangkun/PHD/Project/Fairy-Jev/llm-jev-hybrid/runs/agent_comparison/full_l1_l2_l3_once_20261019T142142+0800/results/scenario_l1_three_cultivar_hn58_action_ready_irrigation__jev__seed1904.json>) |
| LLM + Jev | integration_error | False | False | pre_irrigation_observations=True; new_irrigation_exact_target_and_dose=True; quota_consumption=True; processed_waited_rechecked_response=True; factual_final_report=False | [record](</Users/ouyangkun/PHD/Project/Fairy-Jev/llm-jev-hybrid/runs/agent_comparison/full_l1_l2_l3_once_20261019T142142+0800/results/scenario_l1_three_cultivar_hn58_action_ready_irrigation__hybrid__seed1904.json>) |

## 首个已记录问题的直接证据样本

按层级、架构、错误层、停止原因和工具去重，最多展示30例。该取样方便定位，不代表错误频率；同一运行可能还有后续错误。E编号为原 trace 的 event_id，通常文件行号为E+1，索引同时保存预期行号。

| 层级/架构 | 场景 | 首个问题 | 直接记录 | 来源 |
|---|---|---|---|---|
| L1/原生 Qwen | scenario_l1_three_cultivar_hn58_action_ready_irrigation | tool_selection | Irrigation executed before required visible observations were completed | [E43](</Users/ouyangkun/PHD/Project/Fairy-Jev/llm-jev-hybrid/runs/agent_comparison/full_l1_l2_l3_once_20261019T142142+0800/live/20261019T062251_llm_native_live_9c761560/trace.jsonl>) / [record](</Users/ouyangkun/PHD/Project/Fairy-Jev/llm-jev-hybrid/runs/agent_comparison/full_l1_l2_l3_once_20261019T142142+0800/results/scenario_l1_three_cultivar_hn58_action_ready_irrigation__llm_native__seed1904.json>) |
| L1/纯 Jev | scenario_l1_three_cultivar_hn58_action_ready_irrigation | tool_selection | Irrigation executed before required visible observations were completed | [E47](</Users/ouyangkun/PHD/Project/Fairy-Jev/llm-jev-hybrid/runs/agent_comparison/full_l1_l2_l3_once_20261019T142142+0800/live/20261019T062251_jev_live_4c70b57a/trace.jsonl>) / [record](</Users/ouyangkun/PHD/Project/Fairy-Jev/llm-jev-hybrid/runs/agent_comparison/full_l1_l2_l3_once_20261019T142142+0800/results/scenario_l1_three_cultivar_hn58_action_ready_irrigation__jev__seed1904.json>) |
| L1/LLM + Jev | scenario_l1_three_cultivar_hn58_action_ready_irrigation | model_service | Model HTTP attempt failed: ReadTimeout | [E936](</Users/ouyangkun/PHD/Project/Fairy-Jev/llm-jev-hybrid/runs/agent_comparison/full_l1_l2_l3_once_20261019T142142+0800/live/20261019T062251_hybrid_live_b9951c7f/trace.jsonl>) / [record](</Users/ouyangkun/PHD/Project/Fairy-Jev/llm-jev-hybrid/runs/agent_comparison/full_l1_l2_l3_once_20261019T142142+0800/results/scenario_l1_three_cultivar_hn58_action_ready_irrigation__hybrid__seed1904.json>) |
| L1/LLM + Jev | scenario_l1_hb_coldspring_heihe50_emergence_check | native_tool | {'error': 'Battery 0.0% below minimum 15%'} | [E149](</Users/ouyangkun/PHD/Project/Fairy-Jev/llm-jev-hybrid/runs/agent_comparison/full_l1_l2_l3_once_20261019T142142+0800/live/20261019T062839_hybrid_live_a691298e/trace.jsonl>) / [record](</Users/ouyangkun/PHD/Project/Fairy-Jev/llm-jev-hybrid/runs/agent_comparison/full_l1_l2_l3_once_20261019T142142+0800/results/scenario_l1_hb_coldspring_heihe50_emergence_check__hybrid__seed1904.json>) |
| L1/LLM + Jev | scenario_l1_hb_coldspring_heihe50_field_prep_execution | model_service | Recorded model HTTP attempt failed | [E9](</Users/ouyangkun/PHD/Project/Fairy-Jev/llm-jev-hybrid/runs/agent_comparison/full_l1_l2_l3_once_20261019T142142+0800/live/20261019T063745_hybrid_live_3dbd6775/trace.jsonl>) / [record](</Users/ouyangkun/PHD/Project/Fairy-Jev/llm-jev-hybrid/runs/agent_comparison/full_l1_l2_l3_once_20261019T142142+0800/results/scenario_l1_hb_coldspring_heihe50_field_prep_execution__hybrid__seed1904.json>) |
| L1/原生 Qwen | scenario_l1_hb_coldspring_heihe50_field_prep_execution | model_service | Recorded model HTTP attempt failed | [E47](</Users/ouyangkun/PHD/Project/Fairy-Jev/llm-jev-hybrid/runs/agent_comparison/full_l1_l2_l3_once_20261019T142142+0800/live/20261019T062439_llm_native_live_1904c082/trace.jsonl>) / [record](</Users/ouyangkun/PHD/Project/Fairy-Jev/llm-jev-hybrid/runs/agent_comparison/full_l1_l2_l3_once_20261019T142142+0800/results/scenario_l1_hb_coldspring_heihe50_field_prep_execution__llm_native__seed1904.json>) |
| L1/纯 Jev | scenario_l1_hb_coldspring_heihe50_field_prep_execution | native_tool | {'error': "Already have 'grader' attached."} | [E69](</Users/ouyangkun/PHD/Project/Fairy-Jev/llm-jev-hybrid/runs/agent_comparison/full_l1_l2_l3_once_20261019T142142+0800/live/20261019T062307_jev_live_e5fdaa87/trace.jsonl>) / [record](</Users/ouyangkun/PHD/Project/Fairy-Jev/llm-jev-hybrid/runs/agent_comparison/full_l1_l2_l3_once_20261019T142142+0800/results/scenario_l1_hb_coldspring_heihe50_field_prep_execution__jev__seed1904.json>) |
| L1/原生 Qwen | scenario_l1_hb_coldspring_heihe50_harvest_store_ready | native_tool | {'error': 'Grain bin would overflow: 1307 + 980 kg > 2000 kg. Call unload_grain first.'} | [E158](</Users/ouyangkun/PHD/Project/Fairy-Jev/llm-jev-hybrid/runs/agent_comparison/full_l1_l2_l3_once_20261019T142142+0800/live/20261019T062620_llm_native_live_fa204dfd/trace.jsonl>) / [record](</Users/ouyangkun/PHD/Project/Fairy-Jev/llm-jev-hybrid/runs/agent_comparison/full_l1_l2_l3_once_20261019T142142+0800/results/scenario_l1_hb_coldspring_heihe50_harvest_store_ready__llm_native__seed1904.json>) |
| L1/纯 Jev | scenario_l1_hb_coldspring_heihe50_harvest_store_ready | model_service | Recorded model HTTP attempt failed | [E336](</Users/ouyangkun/PHD/Project/Fairy-Jev/llm-jev-hybrid/runs/agent_comparison/full_l1_l2_l3_once_20261019T142142+0800/live/20261019T062317_jev_live_85ef8feb/trace.jsonl>) / [record](</Users/ouyangkun/PHD/Project/Fairy-Jev/llm-jev-hybrid/runs/agent_comparison/full_l1_l2_l3_once_20261019T142142+0800/results/scenario_l1_hb_coldspring_heihe50_harvest_store_ready__jev__seed1904.json>) |
| L1/纯 Jev | scenario_l1_hb_coldspring_heihe50_planting_execution | native_tool | {'error': 'One or more ridges are already planted'} | [E121](</Users/ouyangkun/PHD/Project/Fairy-Jev/llm-jev-hybrid/runs/agent_comparison/full_l1_l2_l3_once_20261019T142142+0800/live/20261019T062338_jev_live_bc897382/trace.jsonl>) / [record](</Users/ouyangkun/PHD/Project/Fairy-Jev/llm-jev-hybrid/runs/agent_comparison/full_l1_l2_l3_once_20261019T142142+0800/results/scenario_l1_hb_coldspring_heihe50_planting_execution__jev__seed1904.json>) |
| L1/原生 Qwen | scenario_l1_hb_coldspring_heihe50_planting_execution | model_service | Recorded model HTTP attempt failed | [E41](</Users/ouyangkun/PHD/Project/Fairy-Jev/llm-jev-hybrid/runs/agent_comparison/full_l1_l2_l3_once_20261019T142142+0800/live/20261019T062922_llm_native_live_c4f5c76f/trace.jsonl>) / [record](</Users/ouyangkun/PHD/Project/Fairy-Jev/llm-jev-hybrid/runs/agent_comparison/full_l1_l2_l3_once_20261019T142142+0800/results/scenario_l1_hb_coldspring_heihe50_planting_execution__llm_native__seed1904.json>) |
| L1/原生 Qwen | scenario_l1_hb_disease_drought_harvest_ready | native_tool | {'error': 'Grain bin would overflow: 1105 + 980 kg > 2000 kg. Call unload_grain first.'} | [E108](</Users/ouyangkun/PHD/Project/Fairy-Jev/llm-jev-hybrid/runs/agent_comparison/full_l1_l2_l3_once_20261019T142142+0800/live/20261019T064320_llm_native_live_7ec65ed8/trace.jsonl>) / [record](</Users/ouyangkun/PHD/Project/Fairy-Jev/llm-jev-hybrid/runs/agent_comparison/full_l1_l2_l3_once_20261019T142142+0800/results/scenario_l1_hb_disease_drought_harvest_ready__llm_native__seed1904.json>) |
| L1/纯 Jev | scenario_l1_hb_disease_drought_harvest_ready | native_tool | {'error': 'Grain bin would overflow: 1105 + 980 kg > 2000 kg. Call unload_grain first.'} | [E137](</Users/ouyangkun/PHD/Project/Fairy-Jev/llm-jev-hybrid/runs/agent_comparison/full_l1_l2_l3_once_20261019T142142+0800/live/20261019T062406_jev_live_65dd491e/trace.jsonl>) / [record](</Users/ouyangkun/PHD/Project/Fairy-Jev/llm-jev-hybrid/runs/agent_comparison/full_l1_l2_l3_once_20261019T142142+0800/results/scenario_l1_hb_disease_drought_harvest_ready__jev__seed1904.json>) |
| L1/纯 Jev | scenario_l1_hb_disease_drought_mid_fungicide_20_43_action | native_tool | {'error': 'Insufficient fungicide in tank: need 40.0 L, have 0.0 L'} | [E121](</Users/ouyangkun/PHD/Project/Fairy-Jev/llm-jev-hybrid/runs/agent_comparison/full_l1_l2_l3_once_20261019T142142+0800/live/20261019T062419_jev_live_7168c2ec/trace.jsonl>) / [record](</Users/ouyangkun/PHD/Project/Fairy-Jev/llm-jev-hybrid/runs/agent_comparison/full_l1_l2_l3_once_20261019T142142+0800/results/scenario_l1_hb_disease_drought_mid_fungicide_20_43_action__jev__seed1904.json>) |
| L1/纯 Jev | scenario_l1_hb_disease_drought_r5_irrigation_20_43_action | native_tool | {'error': 'Ridge 0 soil VWC 0.341 already >= 0.30'} | [E37](</Users/ouyangkun/PHD/Project/Fairy-Jev/llm-jev-hybrid/runs/agent_comparison/full_l1_l2_l3_once_20261019T142142+0800/live/20261019T062906_jev_live_af3368ea/trace.jsonl>) / [record](</Users/ouyangkun/PHD/Project/Fairy-Jev/llm-jev-hybrid/runs/agent_comparison/full_l1_l2_l3_once_20261019T142142+0800/results/scenario_l1_hb_disease_drought_r5_irrigation_20_43_action__jev__seed1904.json>) |
| L1/纯 Jev | scenario_l1_hb_fertilizer_quota_apply_mild_edge_fertigation | native_tool | {'error': 'insufficient fertilizer or fertilizer quota exceeded: need 120.00 kg'} | [E63](</Users/ouyangkun/PHD/Project/Fairy-Jev/llm-jev-hybrid/runs/agent_comparison/full_l1_l2_l3_once_20261019T142142+0800/live/20261019T062947_jev_live_6e38177e/trace.jsonl>) / [record](</Users/ouyangkun/PHD/Project/Fairy-Jev/llm-jev-hybrid/runs/agent_comparison/full_l1_l2_l3_once_20261019T142142+0800/results/scenario_l1_hb_fertilizer_quota_apply_mild_edge_fertigation__jev__seed1904.json>) |
| L1/纯 Jev | scenario_l1_hb_fertilizer_quota_apply_severe_edge_fertigation | native_tool | {'error': 'insufficient fertilizer or fertilizer quota exceeded: need 120.00 kg'} | [E121](</Users/ouyangkun/PHD/Project/Fairy-Jev/llm-jev-hybrid/runs/agent_comparison/full_l1_l2_l3_once_20261019T142142+0800/live/20261019T063052_jev_live_2a61be15/trace.jsonl>) / [record](</Users/ouyangkun/PHD/Project/Fairy-Jev/llm-jev-hybrid/runs/agent_comparison/full_l1_l2_l3_once_20261019T142142+0800/results/scenario_l1_hb_fertilizer_quota_apply_severe_edge_fertigation__jev__seed1904.json>) |
| L1/纯 Jev | scenario_l1_hb_fertilizer_quota_severe_edge_replant_ready | native_tool | {'error': 'Insufficient None seeds in hopper: need 21440'} | [E89](</Users/ouyangkun/PHD/Project/Fairy-Jev/llm-jev-hybrid/runs/agent_comparison/full_l1_l2_l3_once_20261019T142142+0800/live/20261019T063558_jev_live_44ab239f/trace.jsonl>) / [record](</Users/ouyangkun/PHD/Project/Fairy-Jev/llm-jev-hybrid/runs/agent_comparison/full_l1_l2_l3_once_20261019T142142+0800/results/scenario_l1_hb_fertilizer_quota_severe_edge_replant_ready__jev__seed1904.json>) |
| L1/纯 Jev | scenario_l1_hb_heihe43_apply_targeted_herbicide | native_tool | {'error': 'Insufficient chemical in tank: need 20.0 L, have 10.0 L'} | [E111](</Users/ouyangkun/PHD/Project/Fairy-Jev/llm-jev-hybrid/runs/agent_comparison/full_l1_l2_l3_once_20261019T142142+0800/live/20261019T064324_jev_live_0ff5c972/trace.jsonl>) / [record](</Users/ouyangkun/PHD/Project/Fairy-Jev/llm-jev-hybrid/runs/agent_comparison/full_l1_l2_l3_once_20261019T142142+0800/results/scenario_l1_hb_heihe43_apply_targeted_herbicide__jev__seed1904.json>) |
| L1/纯 Jev | scenario_l1_hb_heihe43_planting_execution | native_tool | {'error': 'One or more ridges are already planted'} | [E137](</Users/ouyangkun/PHD/Project/Fairy-Jev/llm-jev-hybrid/runs/agent_comparison/full_l1_l2_l3_once_20261019T142142+0800/live/20261019T064847_jev_live_9cbc4f93/trace.jsonl>) / [record](</Users/ouyangkun/PHD/Project/Fairy-Jev/llm-jev-hybrid/runs/agent_comparison/full_l1_l2_l3_once_20261019T142142+0800/results/scenario_l1_hb_heihe43_planting_execution__jev__seed1904.json>) |
| L1/纯 Jev | scenario_l1_hb_hn58_dual_stress_harvest_ready | native_tool | {'error': 'Grain bin would overflow: 1040 + 980 kg > 2000 kg. Call unload_grain first.'} | [E127](</Users/ouyangkun/PHD/Project/Fairy-Jev/llm-jev-hybrid/runs/agent_comparison/full_l1_l2_l3_once_20261019T142142+0800/live/20261019T065346_jev_live_0ee7f6e3/trace.jsonl>) / [record](</Users/ouyangkun/PHD/Project/Fairy-Jev/llm-jev-hybrid/runs/agent_comparison/full_l1_l2_l3_once_20261019T142142+0800/results/scenario_l1_hb_hn58_dual_stress_harvest_ready__jev__seed1904.json>) |

## 冻结条件与结论边界

- 每个已执行场景×架构至多 N=1；不能估计稳定成功率、证明架构总体优劣或建立某个设计因素的因果作用。不同任务难度和运行长度混合，延迟统计只作描述。
- 本轮保留已有模型、Prompt、K、候选空间、报告模板和 Agent Loop。既有 Jev 报告候选偏灌溉、有限参数值以及默认等待长度是已登记局限，不能把这些影响单独归因于模型。
- 累计预算按用户授权扩展，原账本行和用量保留；单次固定上限为300工具、300决策轮、900 HTTP、1800秒。原生Loop没有新增决策轮截断，实际受HTTP/工具/墙钟限制。上下文限制维持原值。
- L1/L2从冻结checkpoint对齐初始时钟，L3使用声明时钟；相同场景三架构共享初始化条件。seed1904不代表冻结checkpoint重新生成，也不保证远端采样确定性。
- 有界停止、服务阻塞和中断样本全部保留，不重跑、不替换动作、不放宽验收。当前服务拒绝影响后续依赖该服务的槽位，原始响应和原始轨迹保留。
- 原生validation和workflow比较在运行后进行，oracle只读取静态定义，不执行oracle动作。回放为离线工具回放，不产生模型请求，不能替代真实样本。

## 复核入口

- [全部槽位 CSV](</Users/ouyangkun/PHD/Project/Fairy-Jev/llm-jev-hybrid/runs/agent_comparison/full_l1_l2_l3_once_20261019T142142+0800/slot_index.csv>)：每个预登记槽位的record、run目录、状态及首次问题位置，含pending/blocked。
- [完整证据索引](</Users/ouyangkun/PHD/Project/Fairy-Jev/llm-jev-hybrid/runs/agent_comparison/full_l1_l2_l3_once_20261019T142142+0800/evidence_index.json>)：所有槽位、分组计数、预算快照、来源SHA和原始artifact路径。
- [首次问题样本原字段](</Users/ouyangkun/PHD/Project/Fairy-Jev/llm-jev-hybrid/runs/agent_comparison/full_l1_l2_l3_once_20261019T142142+0800/first_issue_samples.json>)：来自保存评价结果，保留reason/details，不重新判分。
- [预登记](</Users/ouyangkun/PHD/Project/Fairy-Jev/llm-jev-hybrid/runs/agent_comparison/full_l1_l2_l3_once_20261019T142142+0800/preregistration.json>)、[场景清单](</Users/ouyangkun/PHD/Project/Fairy-Jev/llm-jev-hybrid/runs/agent_comparison/full_l1_l2_l3_once_20261019T142142+0800/scenario_inventory.json>)、[预算授权](</Users/ouyangkun/PHD/Project/Fairy-Jev/llm-jev-hybrid/runs/agent_comparison/full_l1_l2_l3_once_20261019T142142+0800/budget_authorization.json>)、[预检](</Users/ouyangkun/PHD/Project/Fairy-Jev/llm-jev-hybrid/runs/agent_comparison/full_l1_l2_l3_once_20261019T142142+0800/preflight_summary.json>)、[原调度汇总](</Users/ouyangkun/PHD/Project/Fairy-Jev/llm-jev-hybrid/runs/agent_comparison/full_l1_l2_l3_once_20261019T142142+0800/comparison.json>)。
- 原始每组 `live/*/` 下保存manifest、trace、workflow、evaluation、metrics、工具目录、参数支持表和验证/回放。读取已脱敏证据；本报告生成器不访问密钥、不联网、不修改原证据。
