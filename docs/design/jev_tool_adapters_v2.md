# Jev 工具适配 v2

在原生工具之上构造Choice及可见反馈约束，执行仍由原ToolExecutor调用原生工具。新增 `parameter_strategy=tool_contracts_v2`；默认策略和v1仍保留，旧配置不改变。

本轮在独立 `jev-tool-adapters-v2` 工作树开发，继承原hybrid工作树已有未提交修改；没有热改正在执行的原生基线。尚未进行v2真实模型实验。

## 使用入口

- 纯Jev：`configs/jev/hn58_jev_tool_adapters_v2.json`，保留task_history_v1。
- Hybrid：`configs/jev/hn58_hybrid_tool_adapters_v2.json`，保留packet策略、Qwen设置及K=4。
- CLI接受 `--parameter-strategy tool_contracts_v2`。
- 离线全场景菜单审查：`PYTHONPATH=. <Python环境>/bin/python scripts/audit_jev_tool_adapters.py --output <新的证据目录>`。

配置仍指向原累计账本；本轮未预留槽位、修改账本或启动模型。两个样例是HN58配置，不应仅修改scenario_id就套用于其他场景；场景批量构建应使用现有config_for_scenario。

## 工具契约与覆盖

61个完整工具实例中，22个无参数；20个范围工具沿用顺序选起点/终点的契约；新增2个单垄契约，剩余17个使用既有枚举、数值和默认值解析。

`FieldOpsApp__irrigate_ridge` 先选ridge_id，再选择完整剩余参数。`FieldOpsApp__apply_pesticide_manual` 同样先选ridge_id，然后联合选择剂量、ridge_count和advance_time；末垄不允许跨出边界，ridge_count按公开docstring限制为1或2。所有原始明确剂量保留；不静默截断以满足255上限。

范围起止顺序、已声明单次宽度和边界继续校验。Robot隐藏的8垄常量没有注入候选；单次执行可能只部分覆盖，后续仍由模型根据反馈选择。

## 可见反馈适配

- **范围**：只使用同一具体工具的covered_ridges/uncovered_ridges；合并连续段，不跨越缺口，不混用虫情、出苗或其他设备观察。候选标注已覆盖、请求中尚未覆盖的范围和来源，不强行禁止复查；“新增请求范围”不是预测本次实际覆盖。
- **等待**：识别effect_ready_at、eta_minutes_to_confirmation、charge_complete_at、eta_minutes_to_full，按观测时间和当前时间计算剩余秒数。相对ETA缺少观测时间时不猜测；后续设备状态替代旧充电状态。
- **设备前置条件**：同一设备最新成功反馈明确表明仍在充电且当前时间未到返回截止时刻，检查参数生成返回needs_context，不执行工具、不发参数请求。完整工具菜单仍保留该工具。控制器通过既有反馈逻辑让模型重新选择，重复拒绝仍受已有失败次数上限约束；不自动等待或替换动作。到期后只解除阻止，不宣称已验证充满。无可靠截止时间则提示信息不足的事实边界，不无限阻止。
- **资源**：施肥候选根据最新公开肥料量与范围×每垄剂量检查。对工具返回一位小数保留0.05kg误差余量；只有即使考虑余量也明显不足才拒绝。后续拖拉机动作未返回该资源值时，旧值失效。燃油、天气、配额、附件与其他资源尚未做完整可行性判断，支持表明确声明，不伪造隐藏常量。
- **报告**：一般任务生成带来源、时间、参数、实际结果投影的事实报告，包含各检查工具覆盖。避免报告递归；省略旧记录/嵌套字段/过长详情时明确记录。包含灌溉动作时额外保留原灌溉专项总结。模板不自由推断农学结论，也不宣告独立验收通过。

## Trace与公平性

每个前缀Choice继续保存选择、候选、hash、与最终工具参数的依赖链；验证器支持v2及单垄契约，非法模型输出仍不替换。资源被排除的候选和来源保存在parameter_candidates中，充电拒绝也记录证据及executed=false。

新增前置条件筛选是明确的策略能力变化，不应把v2结果混入旧v1。工具名称菜单、原生工具执行、任务输入、LLM Prompt、K和验收均不变。本次没有新增无进展自动停止器，没有自动选择“正确下一步”。

`support_matrix.implementation_status` 改为 `resolver_available_not_semantically_certified`，另列具体检查范围，避免将“有解析器”误读为“工具业务适配完成”。

## 验证范围与剩余限制

离线检查覆盖全部246个正式场景的初始菜单、范围分支样本和所有单垄前缀。端到端测试包含纯Jev两种状态、Hybrid真实本地机器人充电与恢复、单垄工具真实执行、HN58 scripted及回放。网络禁止，不把scripted结果当真实模型能力。

参数范围合法不等于任务正确或资源全部可行；候选为有限集合，未知数值/字符串仍可能缺失。当前状态保留窗口之外的历史不会凭空恢复；报告和覆盖标注只声明保留的可见证据。v2未解决上下文token超限、Hybrid摘要保真与同步延迟，后者记录在 [hybrid_llm_followup.md](hybrid_llm_followup.md)。
