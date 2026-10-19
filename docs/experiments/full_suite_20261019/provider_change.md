# 停机与 LLM 提供者切换准备

用户已要求终止实验，并提出更换 Qwen 模型的提供者。旧批次 `full_l1_l2_l3_once_20261019T142142+0800` 已终止：29组保存完整结果、3组中断、706组未启动。实际调用累计5533次HTTP，旧账本累计5599次，原始证据保留。完整结果不等于任务成功，详见同目录 `report.md`。

准备提交 `b4c94d1` 时，新提供者尚未提供。随后用户指定 `http://100.89.223.79:8000/`、`Qwen3.8-27B-FP8`，并确认部署完成。当前配置已切换为 `/v1` 接口、`vllm` 适配器和无需密钥模式。

`fairy.experiments.full_suite` 新增 `prepare --llm-config <公开LLM配置.json>` 入口。该JSON为完整的 `llm` 配置对象，至少包含 `provider`、`model`、`base_url`；环境鉴权还需 `api_key_env`。本地vLLM可使用 `authentication_mode=none`。内嵌密钥、认证头和带认证信息的URL会被拒绝。

该入口仅在准备新批次时覆盖原生LLM与Hybrid的LLM配置，并保存来源与配置哈希；纯Jev配置不变。CLI `prepare` 现在默认读取 `configs/llm/qwen_current.json`；直接调用Python函数 `prepare(out)` 仍需显式传入 `llm_config_path`。独立Hybrid配置 `configs/jev/hn58_hybrid_sync_k4_qwen.json` 同步更新。旧实验冻结配置保持原样。

旧批次存在 `stop_requested.json` 且 `restart_authorized=false`。调度器及单槽位运行入口在触及账本或启动worker前拒绝恢复。更换提供者不自动授权恢复旧实验。

准备阶段禁网回归368项通过；切换后针对 `test_full_suite.py`、`test_local_qwen.py` 的45项禁网检查通过。真实接口检查保存在 `runs/provider_checks/qwen38_ready_20261019T162202+0800/`：模型登记GET为200，工具调用格式和现有Hybrid updater材料格式两次请求均200，实际响应模型均为 `Qwen3.8-27B-FP8`。两次请求计入原累计账本，总数5599→5601；该槽位标记为provider_connectivity，不是任务样本。无Agent运行、无工具执行、无Jev请求。未加载env.sh，SDK只使用公开EMPTY占位符，沿用127.0.0.1:7897代理。

部署前一次GET返回502、一次直接路由GET连接被拒绝，原证据保留在 `runs/provider_checks/qwen38_20261019T160719+0800/`。部署后验证通过不能证明完整任务成功。

用户随后明确授权在额外新目录重跑246场景×3实现。该授权仅启动新的独立批次；旧批次继续保持停止。新计划见 `../full_suite_qwen38_fresh_20261019/plan.md`。
