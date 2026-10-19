# Qwen3.8：全量三架构新批次

2026-10-19用户明确要求：“执行全量实验……之前的历史记录不要复用，直接额外构建目录重跑”。本轮在独立目录 `runs/agent_comparison/full_l1_l2_l3_qwen38_fresh_20261019T163336+0800/` 开始全部新样本，覆盖正式78个L1、78个L2、90个L3场景，三种实现各一次，共738个槽位。旧批次和原始轨迹不改动，不复制旧样本、模型材料、工具结果或已完成标记作为新结果。

原生LLM与Hybrid updater共同使用 `configs/llm/qwen_current.json`：`http://100.89.223.79:8000/v1`、`Qwen3.8-27B-FP8`、无需密钥。保留temperature0.1、max_output_tokens2048、parallel_tool_calls=true、enable_thinking=false、timeout30。纯Jev仍使用既有TypeSafe配置。

沿用现有场景初始化、seed1904、Prompt、K=4、工具、参数候选、报告模板、Agent Loop与验收规则。同一场景的三个新样本分别初始化独立环境。历史配置只作为共同设置基线，不带入历史模型状态或动作；远端未配置采样seed。

每个新槽位仅运行一次，不因失败追加替代样本。每次上限300工具、300个Jev决策轮、600个Jev Choice、900次HTTP、80次Hybrid更新、1800秒；最多三个进程且每架构最多一个。累计账本保留已有用量，并按新授权扩容；不会把账本历史用量算成本轮样本结果。

HN58保留原semantic_v2断言；其他场景主task_success仍为unknown/null，保存原生validator与workflow指标，不新增成功阈值。完整运行不等于任务通过。服务访问失败按既有规则保留证据并停止受影响的新槽位，不改变网络出口。

先执行当前代码的禁网回归和246场景独立fixture检查，再启动新批次。实时查看批次 `progress.json`、`comparison.json`、`report.md`。`live/` 保存所有真实请求响应、动作及环境反馈；最终提供独立报告与完整证据索引。
