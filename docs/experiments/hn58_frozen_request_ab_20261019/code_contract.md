# T5：原生终止契约（只读）

执行源码版本：`bbe762c98a97e5f22cceb5f7b24fc3a67656fdb6`。从git show提取的原字节快照保存在source_code/，SHA记录于source_identity.json；相关当前源码与原执行版本逐字相同。本轮没有初始化Engine/Agent。

## 实现事实 · DIRECT_OBSERVATION

1. [fairy/agents/agent/base_agent.py:64](/Users/ouyangkun/PHD/Project/Fairy-Jev/llm-jev-hybrid/runs/agent_comparison/hn58_premature_stop_frozen_request_20261019T012154+0800/source_code/fairy__agents__agent__base_agent.py:64) BaseAgent.chat_completion 调用LLM，108–110行取response.choices[0].message及原始tool_calls，然后应用tool-call policy；125–129行记录raw/effective/dropped数量。
2. [fairy/agents/agent/base_agent.py:139](/Users/ouyangkun/PHD/Project/Fairy-Jev/llm-jev-hybrid/runs/agent_comparison/hn58_premature_stop_frozen_request_20261019T012154+0800/source_code/fairy__agents__agent__base_agent.py:139) _enforce_tool_call_policy 在parallel_tool_calls=true时直接返回原message。原样本raw/effective/dropped计数均0，没有将生成的工具调用丢弃。
3. [fairy/agents/agent/agent.py:346](/Users/ouyangkun/PHD/Project/Fairy-Jev/llm-jev-hybrid/runs/agent_comparison/hn58_premature_stop_frozen_request_20261019T012154+0800/source_code/fairy__agents__agent__agent.py:346) Agent.run 调用_chat_completion_with_retries，随后after_llm_message；386–388行仅检查 `if not message.tool_calls`，设置 `agent_finished` 并break。该条件不检查自然语言中承诺的未来动作，也不调用任务验收器。
4. [fairy/experiments/native.py:145](/Users/ouyangkun/PHD/Project/Fairy-Jev/llm-jev-hybrid/runs/agent_comparison/hn58_premature_stop_frozen_request_20261019T012154+0800/source_code/fairy__experiments__native.py:145) run_native 仍调用原Engine→Agent.run，并将stop_reason写入native_run_end；观测wrapper没有人为补动作。

原始事件链：E49（JSONL第50行）raw_response_body → E50 SDK parsed_response → E52（第53行）native llm_call_end → E55（第56行）native_run_end。三处assistant内容完全相同，工具列表均空；E55是agent_finished。见termination_chain.json。原E49 finish_reason=stop、completion_tokens=196、max_tokens=2048，没有length截断、解析器丢调用或工具不可用证据。finish_reason只结束该次生成，不证明环境任务完成。

捕获的原system已包含 `Continue until the task is complete or impossible with available tools.` 和 `Only message the user when the full task is done or cannot be completed.`。因此不能说原提示完全缺少续执行要求。新B规则仅澄清不得用未来计划替代未执行动作。

## 等待工具的静态语义

- [fairy/apps/system.py:62](/Users/ouyangkun/PHD/Project/Fairy-Jev/llm-jev-hybrid/runs/agent_comparison/hn58_premature_stop_frozen_request_20261019T012154+0800/source_code/fairy__apps__system.py:62) SystemApp.advance_time 用整数秒/分/时/天计算总秒数，total_seconds<=0返回错误；合法正值推进仿真时钟并触发相关物理时间推进。返回hours=2是schema合法的7200秒等待意图。本诊断只验证它，没有执行。
- [fairy/apps/system.py:121](/Users/ouyangkun/PHD/Project/Fairy-Jev/llm-jev-hybrid/runs/agent_comparison/hn58_premature_stop_frozen_request_20261019T012154+0800/source_code/fairy__apps__system.py:121) wait_for_notification 本身设置timeout再调用Engine回调；[fairy/controllers/engine.py:127](/Users/ouyangkun/PHD/Project/Fairy-Jev/llm-jev-hybrid/runs/agent_comparison/hn58_premature_stop_frozen_request_20261019T012154+0800/source_code/fairy__controllers__engine.py:127) Engine将时钟跳到更早的下一动态事件或timeout。正timeout可产生等待进展，但可能在灌溉ETA之前返回；默认timeout=0不保证推进。因此不能无条件当作完成两小时等待的等价替代。本轮四次回复均未选择该工具。

## 可选架构调整 · HYPOTHESIS，未实施

后续若需要，可单独设计全闭环验证或终止前完成状态检查；必须作为新的实验。这里未改Loop自动续跑、未插入工具、未让evaluator私有真值进入Agent。模型为何忽略原通用续执行要求，无法从这些轨迹读取其内部原因。
