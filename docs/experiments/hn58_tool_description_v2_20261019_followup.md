# 历史451门禁修正与后续实验取消

2026-10-19，用户指出：后续已有同一服务真实成功证据，不应继续用旧451永久阻止Jev/Hybrid。本次修正 `fairy/experiments/access.py`：更晚、同服务及同endpoint的配对HTTP200成功证据可使历史451退出当前阻塞列表，原始拒绝及其解除证据仍保留。不能仅凭缺失时间戳、未配对事件、另一模型/endpoint或更早成功解除；后续新451仍有效。401/403及预算规则不变。Prompt、K、候选、Loop、报告模板和验收均未修改。

现存迁移后Hybrid run `20261018T140025_hybrid_live_9fd09d81` 的真实HTTP200满足此规则。修正后的离线分类中三架构均无该历史451阻塞。没有通过删除旧证据或发送额外探测请求完成判断。

随后用户明确要求不再执行后续实验。在任何新的live或scripted episode启动之前已停止；Jev、Hybrid没有新样本，原生Qwen没有重跑。已完成的只有门禁修正及离线单元测试：`.venv/bin/python -m pytest -q tests/jev/test_access.py`，29 passed in 0.07s。

同一账本仍为7/12 episodes、33/1440 HTTP，新增消耗为0。上轮203个证据文件（含压缩包及SHA文件）哈希保持不变。取消记录在 `runs/agent_comparison/hn58_tool_description_v2_20261019_continuation/cancellation.json`。

原始批次报告中的blocked保留为当时实际状态；它不再表示当前门禁仍拒绝运行。当前不执行后续实验的原因是用户取消。上轮原生Qwen结果不变：真实收到配额、正确灌溉，但没有执行等待和复查；`live_integration_success=true`、`task_success=false`。这证明该样本的执行闭环仍不完整，不据此推断其他未测试架构的结果或唯一原因。
