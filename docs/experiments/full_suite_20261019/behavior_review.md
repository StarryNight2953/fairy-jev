# 全量 L1/L2/L3：已完成样本行为复核

生成时间：2026-10-19T15:08:25.487553+08:00。
本快照读取 29 个已完成 live 样本；预登记 738 个。结果记录 738 个，另有 706 个无 live 路径、3 个待读取；这些记录不计入下列行为统计。

此文件只描述已保存的真实请求、工具行为和停止机制。原 task_success、原生合同和评分器首次问题均原样保留。unknown 没有被转成通过或失败。

工具错误可能被恢复；没有通信不自动等于任务失败；FINISH 不自动证明完成。‘固定灌溉模板错配’只按公开任务关键词筛查，需人工复核。候选失败仅指 trace 中真实记录的 needs_context/unsupported/limit，不推断未提供候选的必需性。

## 分等级与架构

|等级/架构|样本|有技术/候选错误|候选失败|触及限制|无实际通信|模板待复核|HTTP|工具|
|---|---:|---:|---:|---:|---:|---:|---:|---:|
|L1/hybrid|5|5|0|0|2|2|371|209|
|L1/jev|18|16|0|6|10|6|4242|2178|
|L1/llm_native|6|4|0|0|2|0|156|127|

## 逐样本定位

链接指向原 trace。E 为原 event_id，行号另外保存在 JSON，末尾12条事件与末尾12条行为窗口也在 JSON。

|等级/架构|场景|停止原因|原任务结果|实际通信|错误/候选错误|模板待复核|首次技术或候选错误|
|---|---|---|---|---:|---:|---|---|
|L1/hybrid|scenario_l1_hb_coldspring_heihe50_emergence_check|llm_update_error|unknown|1|15/0|是|[tool E149](</Users/ouyangkun/PHD/Project/Fairy-Jev/llm-jev-hybrid/runs/agent_comparison/full_l1_l2_l3_once_20261019T142142+0800/live/20261019T062839_hybrid_live_a691298e/trace.jsonl#L150>)|
|L1/jev|scenario_l1_hb_coldspring_heihe50_emergence_check|model_finish|unknown|0|0/0||未观察到（不表示任务成功）|
|L1/llm_native|scenario_l1_hb_coldspring_heihe50_emergence_check|agent_finished|unknown|1|0/0||未观察到（不表示任务成功）|
|L1/hybrid|scenario_l1_hb_coldspring_heihe50_field_prep_execution|model_finish|unknown|0|3/0||[model_http E9](</Users/ouyangkun/PHD/Project/Fairy-Jev/llm-jev-hybrid/runs/agent_comparison/full_l1_l2_l3_once_20261019T142142+0800/live/20261019T063745_hybrid_live_3dbd6775/trace.jsonl#L10>)|
|L1/jev|scenario_l1_hb_coldspring_heihe50_field_prep_execution|model_finish|unknown|0|1/0||[tool E69](</Users/ouyangkun/PHD/Project/Fairy-Jev/llm-jev-hybrid/runs/agent_comparison/full_l1_l2_l3_once_20261019T142142+0800/live/20261019T062307_jev_live_e5fdaa87/trace.jsonl#L70>)|
|L1/llm_native|scenario_l1_hb_coldspring_heihe50_field_prep_execution|agent_finished|unknown|1|3/0||[model_http E47](</Users/ouyangkun/PHD/Project/Fairy-Jev/llm-jev-hybrid/runs/agent_comparison/full_l1_l2_l3_once_20261019T142142+0800/live/20261019T062439_llm_native_live_1904c082/trace.jsonl#L48>)|
|L1/hybrid|scenario_l1_hb_coldspring_heihe50_harvest_store_ready|llm_update_error|unknown|5|3/0|是|[model_http E1172](</Users/ouyangkun/PHD/Project/Fairy-Jev/llm-jev-hybrid/runs/agent_comparison/full_l1_l2_l3_once_20261019T142142+0800/live/20261019T064037_hybrid_live_11a0a087/trace.jsonl#L1173>)|
|L1/jev|scenario_l1_hb_coldspring_heihe50_harvest_store_ready|decision_error|unknown|0|2/0||[model_http E336](</Users/ouyangkun/PHD/Project/Fairy-Jev/llm-jev-hybrid/runs/agent_comparison/full_l1_l2_l3_once_20261019T142142+0800/live/20261019T062317_jev_live_85ef8feb/trace.jsonl#L337>)|
|L1/llm_native|scenario_l1_hb_coldspring_heihe50_harvest_store_ready|agent_finished|unknown|1|3/0||[tool E158](</Users/ouyangkun/PHD/Project/Fairy-Jev/llm-jev-hybrid/runs/agent_comparison/full_l1_l2_l3_once_20261019T142142+0800/live/20261019T062620_llm_native_live_fa204dfd/trace.jsonl#L159>)|
|L1/hybrid|scenario_l1_hb_coldspring_heihe50_planting_execution|llm_update_error|unknown|0|4/0||[model_http E76](</Users/ouyangkun/PHD/Project/Fairy-Jev/llm-jev-hybrid/runs/agent_comparison/full_l1_l2_l3_once_20261019T142142+0800/live/20261019T064632_hybrid_live_1bfb5734/trace.jsonl#L77>)|
|L1/jev|scenario_l1_hb_coldspring_heihe50_planting_execution|decision_error|unknown|0|6/0||[tool E121](</Users/ouyangkun/PHD/Project/Fairy-Jev/llm-jev-hybrid/runs/agent_comparison/full_l1_l2_l3_once_20261019T142142+0800/live/20261019T062338_jev_live_bc897382/trace.jsonl#L122>)|
|L1/llm_native|scenario_l1_hb_coldspring_heihe50_planting_execution|native_error|unknown|0|22/0||[model_http E41](</Users/ouyangkun/PHD/Project/Fairy-Jev/llm-jev-hybrid/runs/agent_comparison/full_l1_l2_l3_once_20261019T142142+0800/live/20261019T062922_llm_native_live_c4f5c76f/trace.jsonl#L42>)|
|L1/jev|scenario_l1_hb_disease_drought_harvest_ready|decision_error|unknown|0|3/0||[tool E137](</Users/ouyangkun/PHD/Project/Fairy-Jev/llm-jev-hybrid/runs/agent_comparison/full_l1_l2_l3_once_20261019T142142+0800/live/20261019T062406_jev_live_65dd491e/trace.jsonl#L138>)|
|L1/llm_native|scenario_l1_hb_disease_drought_harvest_ready|native_error|unknown|0|25/0||[tool E108](</Users/ouyangkun/PHD/Project/Fairy-Jev/llm-jev-hybrid/runs/agent_comparison/full_l1_l2_l3_once_20261019T142142+0800/live/20261019T064320_llm_native_live_7ec65ed8/trace.jsonl#L109>)|
|L1/jev|scenario_l1_hb_disease_drought_mid_fungicide_20_43_action|max_steps|unknown|283|2/0||[tool E121](</Users/ouyangkun/PHD/Project/Fairy-Jev/llm-jev-hybrid/runs/agent_comparison/full_l1_l2_l3_once_20261019T142142+0800/live/20261019T062419_jev_live_7168c2ec/trace.jsonl#L122>)|
|L1/jev|scenario_l1_hb_disease_drought_r5_irrigation_20_43_action|model_finish|unknown|0|9/0||[tool E37](</Users/ouyangkun/PHD/Project/Fairy-Jev/llm-jev-hybrid/runs/agent_comparison/full_l1_l2_l3_once_20261019T142142+0800/live/20261019T062906_jev_live_af3368ea/trace.jsonl#L38>)|
|L1/jev|scenario_l1_hb_disease_drought_whole_field_planting_execution|decision_error|unknown|0|5/0||[tool E159](</Users/ouyangkun/PHD/Project/Fairy-Jev/llm-jev-hybrid/runs/agent_comparison/full_l1_l2_l3_once_20261019T142142+0800/live/20261019T062919_jev_live_7044ac5d/trace.jsonl#L160>)|
|L1/jev|scenario_l1_hb_fertilizer_quota_apply_mild_edge_fertigation|decision_error|unknown|0|57/0||[tool E63](</Users/ouyangkun/PHD/Project/Fairy-Jev/llm-jev-hybrid/runs/agent_comparison/full_l1_l2_l3_once_20261019T142142+0800/live/20261019T062947_jev_live_6e38177e/trace.jsonl#L64>)|
|L1/jev|scenario_l1_hb_fertilizer_quota_apply_severe_edge_fertigation|max_steps|unknown|259|8/0|是|[tool E121](</Users/ouyangkun/PHD/Project/Fairy-Jev/llm-jev-hybrid/runs/agent_comparison/full_l1_l2_l3_once_20261019T142142+0800/live/20261019T063052_jev_live_2a61be15/trace.jsonl#L122>)|
|L1/jev|scenario_l1_hb_fertilizer_quota_harvest_store_ready|decision_error|unknown|0|6/0||[tool E111](</Users/ouyangkun/PHD/Project/Fairy-Jev/llm-jev-hybrid/runs/agent_comparison/full_l1_l2_l3_once_20261019T142142+0800/live/20261019T063527_jev_live_30d32ca2/trace.jsonl#L112>)|
|L1/jev|scenario_l1_hb_fertilizer_quota_severe_edge_replant_ready|decision_error|unknown|109|3/0|是|[tool E89](</Users/ouyangkun/PHD/Project/Fairy-Jev/llm-jev-hybrid/runs/agent_comparison/full_l1_l2_l3_once_20261019T142142+0800/live/20261019T063558_jev_live_44ab239f/trace.jsonl#L90>)|
|L1/jev|scenario_l1_hb_heihe43_apply_nutrient_fertigation|max_steps|unknown|225|1/0|是|[tool E1583](</Users/ouyangkun/PHD/Project/Fairy-Jev/llm-jev-hybrid/runs/agent_comparison/full_l1_l2_l3_once_20261019T142142+0800/live/20261019T063813_jev_live_87137c75/trace.jsonl#L1584>)|
|L1/jev|scenario_l1_hb_heihe43_apply_targeted_herbicide|max_steps|unknown|36|2/0|是|[tool E111](</Users/ouyangkun/PHD/Project/Fairy-Jev/llm-jev-hybrid/runs/agent_comparison/full_l1_l2_l3_once_20261019T142142+0800/live/20261019T064324_jev_live_0ff5c972/trace.jsonl#L112>)|
|L1/jev|scenario_l1_hb_heihe43_harvest_store_ready|decision_error|unknown|0|2/0||[model_http E406](</Users/ouyangkun/PHD/Project/Fairy-Jev/llm-jev-hybrid/runs/agent_comparison/full_l1_l2_l3_once_20261019T142142+0800/live/20261019T064821_jev_live_f3dc3f54/trace.jsonl#L407>)|
|L1/jev|scenario_l1_hb_heihe43_planting_execution|max_steps|unknown|270|4/0|是|[tool E137](</Users/ouyangkun/PHD/Project/Fairy-Jev/llm-jev-hybrid/runs/agent_comparison/full_l1_l2_l3_once_20261019T142142+0800/live/20261019T064847_jev_live_9cbc4f93/trace.jsonl#L138>)|
|L1/jev|scenario_l1_hb_hn58_dual_stress_harvest_ready|max_steps|unknown|285|1/0|是|[tool E127](</Users/ouyangkun/PHD/Project/Fairy-Jev/llm-jev-hybrid/runs/agent_comparison/full_l1_l2_l3_once_20261019T142142+0800/live/20261019T065346_jev_live_0ee7f6e3/trace.jsonl#L128>)|
|L1/hybrid|scenario_l1_three_cultivar_hn58_action_ready_irrigation|llm_update_error|false|18|3/0||[model_http E936](</Users/ouyangkun/PHD/Project/Fairy-Jev/llm-jev-hybrid/runs/agent_comparison/full_l1_l2_l3_once_20261019T142142+0800/live/20261019T062251_hybrid_live_b9951c7f/trace.jsonl#L937>)|
|L1/jev|scenario_l1_three_cultivar_hn58_action_ready_irrigation|model_finish|false|2|0/0||未观察到（不表示任务成功）|
|L1/llm_native|scenario_l1_three_cultivar_hn58_action_ready_irrigation|agent_finished|false|1|0/0||未观察到（不表示任务成功）|

## 复核入口与限制

- `behavior_review.json`：逐样本完整通信文本、错误摘要、候选生成结果、末尾窗口、原评分及各文件 SHA256。
- `first_technical_or_candidate_error` 是本脚本定位的技术/执行问题；`original_evaluator_first_issue` 保留原评分器结论，二者范围不同。
- HTTP 错误和随后 decision 错误可能是同一条失败链；错误事件数量不是独立故障数量。
- `declared_parameter_catalogs` 原样保留已运行版本声明的公共候选值，按哈希去重；没有根据任务内容推断或补充缺少的值。
- 聚合延迟为各 episode 墙钟时间之和，不是并行批次总耗时；模型 tokens 为服务上报值，没有据此估算费用。
- 现有 native 合同与工作流相似度不等于独立任务完成验收。没有给其他场景临时制定成功规则。
- 输入文件只读；本脚本不导入 FAIRY，不执行环境或工具，不访问模型。重复执行仅更新当前 analysis 目录。
