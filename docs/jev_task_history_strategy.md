# 纯Jev第二种输入策略：task_history_v1

当前纯Jev可以选择两种输入策略。默认 `visible_state_v1` 保留原有行为；`task_history_v1` 是新增的“初始任务＋完整历史调用”方式。Hybrid继续使用自己的packet/fresh-events输入，不能启用纯Jev的这一选项。

切换方式：

```sh
PYTHONPATH=. .venv/bin/python -m fairy.agents.Jev \
  --mode scripted \
  --config configs/jev/hn58_irrigation.json \
  --state-strategy task_history_v1 \
  --output-dir runs/jev/task_history_scripted
```

也可以直接使用 `configs/jev/hn58_task_history.json`，或在传给 `run_episode(..., architecture="jev")` 的配置中设置 `"state_strategy": "task_history_v1"`。省略此字段保持旧策略；显式 `visible_state_v1` 切回原有方式。未知策略会被拒绝。上述示例使用scripted后端，只有离线工具闭环，不调用模型服务。

新策略的模型输入为：

```text
task                         初始任务原文
actions_so_far[]              全部工具调用，按实际观察顺序排列
  observation_id             与通知共享的顺序标识
  action / arguments / result 原工具名、参数、完整返回
  status / executed          成功、错误、是否实际执行
  sim_time_before/after      工具调用前后的仿真时间
  error / error_stage        原始错误详情（存在时）
current_time                 本轮可见时间
system_context               可见系统上下文
environment_notifications[]  全部可见环境通知及原有时间、顺序标识
controller_feedback[]       全部候选构造/控制器反馈
```

所有工具历史均保留，结果在对应动作中出现一次；相同工具重复读取、重复报告也不会被合并。没有截断、摘要、LLM更新器或超限后缩减重试。旧配置中的history条数限制继续用于候选生成的原始可见状态，不限制新策略送给Jev的完整历史。

该设计参考本地 `Reference/fairy-jev-jev-agent/fairy/agents/jev/jev_agent.py` 的 `_state()`。参考实现仅返回task和action/arguments/result；本实现额外保留通知、时间、执行状态及反馈，避免重新引入等待后看不到环境通知的问题。因此这是借鉴其历史组织方式的实现，并非逐字复制参考项目。当前工作流成功工具节点为READ/WRITE，新投影直接读取完整VisibleState记录，避免参考项目TOOL筛选条件遗漏当前成功调用。

模型仍按原有两阶段选择：全量工具菜单→选中工具的完整参数组合。参数候选继续读取旧 `VisibleState.snapshot()`；同轮两次Choice读取同一份冻结的新state。Prompt、候选生成、执行循环、报告模板、验收规则和FINISH语义均未改变。新策略及输入尺寸记录在trace与manifest中，尺寸为字符/UTF-8字节，不能当作模型token数。

**完整历史不等于上下文压缩。** 它减少了旧输入中的审计元数据及重复组织，但也重新带入旧策略已经省略的读结果和重复报告。短轨迹可能变小，长轨迹可能显著变大；是否暂时避开某次服务端超限必须基于完整请求和实际响应判断。它不构成通用的上下文预算或恢复机制。
