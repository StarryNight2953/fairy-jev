# 新纯Jev task_history_v1：L1真实运行各一次

用户明确选择“仅尝试新实现的jev，此前的jev暂时不必测试”。本批只运行新纯Jev task_history_v1，覆盖全部78个正式L1场景，每场景一次，共78个新槽位。原纯Jev、原生Qwen和Hybrid不启动。旧结果只用于事后的描述性对照，不填充本轮样本。

统一seed=1904及各场景checkpoint初始条件。模型为TypeSafe jev-1.13.0；使用当前任务输入和数值候选修复，以及任务＋完整调用历史state。单场上限7200秒，Jev单次请求仍为30秒；工具/决策/HTTP次数保持当前上限。Prompt、候选规则、Agent Loop、报告模板和任务验收在批次开始后冻结不改。Qwen的1800秒请求配置与本批无关，因为本批不调用Qwen。

纯Jev串行运行，每次只有一个场景episode在执行。没有失败补测、动作替换或旧批次重启。当前访问错误按服务阻止后续依赖槽位并保留原始证据。共享预算保留全部历史，本批在现有容量内，无需重置或提高上限。

只有HN58具有现有严格语义验收；其他77个L1的task_success继续为unknown，另记录原生validator、工具行为、错误、轨迹指标和集成结果，不据此计算普适任务成功率。报告记录上下文超限、首次错误、延迟与usage，并与旧批次相同场景作描述性比较。因本轮同时包含此前输入/数值候选修复，且每场景仅一个样本，不能将所有变化唯一归因于新state。

批次目录：`/Users/ouyangkun/PHD/Project/Fairy-Jev/llm-jev-hybrid/runs/agent_comparison/l1_task_history_once_20261019T212250+0800`

- [实时进度](/Users/ouyangkun/PHD/Project/Fairy-Jev/llm-jev-hybrid/runs/agent_comparison/l1_task_history_once_20261019T212250+0800/progress.json)
- [持续更新的报告](/Users/ouyangkun/PHD/Project/Fairy-Jev/llm-jev-hybrid/runs/agent_comparison/l1_task_history_once_20261019T212250+0800/report.md)
- [预登记与全部槽位](/Users/ouyangkun/PHD/Project/Fairy-Jev/llm-jev-hybrid/runs/agent_comparison/l1_task_history_once_20261019T212250+0800/preregistration.json)
- [逐组原始证据索引](/Users/ouyangkun/PHD/Project/Fairy-Jev/llm-jev-hybrid/runs/agent_comparison/l1_task_history_once_20261019T212250+0800/evidence_index.json)

此文件记录启动设计，不将准备通过或批次启动描述为任务完成。冻结提交、启动进程、配置哈希、离线回归和78场景预检查结果分别保存在批次内。
