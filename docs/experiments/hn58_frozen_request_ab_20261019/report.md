# HN58原生Qwen：冻结第三轮请求A/B诊断

日期：2026-10-19（Asia/Shanghai）。T0–T6全部通过。诊断运行提交 `3eec3d6b11c1907b793ed91f0e67dd674d5684d7`；原真实运行提交 `bbe762c98a97e5f22cceb5f7b24fc3a67656fdb6`。报告后续提交与运行提交分开。

**DIRECT_OBSERVATION：A两次均停在未来计划文本；B两次均实际返回合法 `SystemApp__advance_time({"hours":2})` 工具调用。四次HTTP均200，同一响应模型；所有工具执行次数为0。**

| 顺序 | 条件 | 分类 | 实际tool_calls | finish_reason | HTTP秒 | 输入/输出tokens | cached tokens |
| --- | --- | --- | --- | --- | ---: | ---: | ---: |
| 1 | A | text_only_future_plan | 空 | stop | 10.898 | 13822/239 | null |
| 2 | B | progress_tool_call | advance_time(hours=2) | tool_calls | 10.034 | 13878/283 | 8448 |
| 3 | B | progress_tool_call | advance_time(hours=2) | tool_calls | 8.174 | 13878/306 | 13728 |
| 4 | A | text_only_future_plan | 空 | stop | 6.508 | 13822/250 | 13728 |

每组n=2：A progress_tool_call=0、text_only_future_plan=2；B progress_tool_call=2、text_only_future_plan=0。两组other_or_invalid_tool_call、text_only_claimed_complete、request_error均0。原E49仅为历史观察，不计入A。A中的“操作完成”指已做灌溉，并仍要求未来等待复查，按预注册优先判为future_plan，原文完整保留。

## 输入与终止机制

A直接来自源trace E48（JSONL第49行），9条消息、58个工具；历史tool_call_id及单引号字典字符串原样保留。B只在messages[0].content的agent_instructions节末尾新增：

> - If the task still requires a tool action, use the available tools to do that action now. Do not replace an unexecuted action with a plan for later. Give a final user response only after required actions are complete, or when a tool limitation prevents further action.

无其他JSON值变化。temperature=0.1、max_tokens=2048、parallel_tool_calls=true、enable_thinking=false及工具顺序、任务、历史均相同；没有tool_choice=required或新用户消息。四次实际发送正文与各自冻结文件相等；发送前还逐字节检查了固定SDK wire SHA。见request_diff.json、A/B_request.json及每个samples/NN/trace.jsonl。SDK仍为OpenAI2.44.0；为遵守本次4HTTP硬上限，唯一协议层调整是禁用SDK重试，并加一次发送保护；没有改变生成参数。

原system **已经**要求 `Continue until the task is complete or impossible with available tools.`，以及 `Only message the user when the full task is done or cannot be completed.`。本次测试的是进一步明确“未执行动作不能被未来计划替代”，不是补上原本不存在的续执行要求。

T5证实原E49空tool_calls原样进入E50/E52，dropped_tool_call_count=0；Agent.run 386–388行遇空tool_calls就agent_finished，E55记录同一停止原因。没有证据表明输出截断或工具不可用；finish_reason=stop不等于任务完成。源码快照、函数位置及链路见code_contract.md、termination_chain.json。

## 行为质量与结论边界

- **DIRECT_OBSERVATION**：B确实生成了结构化工具调用，不只是用更强语气描述未来计划。两次均为整数hours=2（7200秒），已在冻结公开schema与原SystemApp参数规则下验证；没有选错工具、无效参数、重复灌溉或额外无关动作。
- **SUPPORTED_INFERENCE**：这条通用续执行规则可能影响这个冻结上下文中的局部“调用工具还是结束回复”选择，结果支持提示敏感性。
- **HYPOTHESIS**：相同规则可能减少真实Loop中的提前结束，但本轮没有做这个全闭环检验。
- **UNKNOWN**：不能确认唯一因果、统计显著性、总体成功率或其他任务/模型上的效果。每组仅2次，temperature非零且远端sampling seed未提供；ABBA为固定顺序，服务缓存状态不同，不能据延迟推断效率。

即使返回advance_time，也只是未执行的等待意图。未取得实际等待结果、后续土壤/目标区复查、完整43–63覆盖或合格最终报告，**task_success=null（未验收）**，没有任何task_success=true。B的文字仍提及S5/S6，若仅执行那样的未来复查可能漏43垄；本次没有生成或执行该复查，不能称为已发生的覆盖错误。

这是“已查询配额并灌溉后，描述未来行动但不调用工具”的终止问题，与早期“灌溉前漏查配额”属于不同失效。冻结E48已含真实配额与灌溉结果，没有改写或移除它们。

## 服务、预算与保全

全部响应model为Qwen3.6-35B-A3B-FP8、fingerprint为vllm-0.22.0-bbe13ed8，与原记录一致。endpoint沿用 http://100.115.106.71:8000/v1/chat/completions ，SDK仍选择127.0.0.1:7897系统代理；没有连接探测、更换出口、访问Jev或读取env.sh/真实密钥。原配置authentication_mode=none，SDK仅使用公开EMPTY占位，不记录认证头。

预注册顺序A→B→B→A，实际4HTTP，无重试或补样。每个请求通过现有LiveLedger.reserve_episode及EpisodeRuntime→ObservedTransport.reserve_http计账，消耗一个episode**预算槽位**，不表示运行了Agent episode。统一账本从7/12槽位、33/1440HTTP到 **11/12槽位、37/1440HTTP**；旧7条记录及上限不变。没有新账本。环境初始化、工具执行、完整Agent运行、Jev请求均为0。费用、远端seed及未提供的缓存用量保留null。

保全重新核对1657个历史证据/生产文件，均未变化；原冻结tag、workspace映射、原生Loop及工具实现未改。新增提交只含诊断脚本、离线单元测试与文档。22项离线测试通过，测试使用MockTransport和临时测试账本，不产生服务请求。

阅读入口：summary.json查看逐组统计；samples.jsonl查看逐样本分类和完整响应路径；samples/01–04/含真实请求/响应trace、raw_response.txt及前后预算。source_identity.json和evidence_index.json提供SHA/事件行号；files.sha256覆盖整个新证据包。全部源E43/E48/E49/E52/E55及源码快照保留。

后续仅建议：如另行开展全闭环验证，应独立记录等待后的复查覆盖和报告结果。本次到此结束，不自动实施或继续采样。
