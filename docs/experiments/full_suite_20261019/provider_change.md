# 停机与 LLM 提供者切换准备

用户已要求终止实验，并提出更换 Qwen 模型的提供者。旧批次 `full_l1_l2_l3_once_20261019T142142+0800` 已终止：29组保存完整结果、3组中断、706组未启动。实际调用累计5533次HTTP，旧账本累计5599次，原始证据保留。完整结果不等于任务成功，详见同目录 `report.md`。

截至本次提交，新提供者的 Base URL、模型ID和鉴权环境变量名尚未提供，因此**尚未切换提供者，也没有进行新接口调用**。密钥仅存放在本地环境配置中，不进入公开实验配置或Git。

`fairy.experiments.full_suite` 新增 `prepare --llm-config <公开LLM配置.json>` 入口。该JSON为完整的 `llm` 配置对象，至少包含 `provider`、`model`、`base_url`；环境鉴权还需 `api_key_env`。本地vLLM可使用 `authentication_mode=none`。内嵌密钥、认证头和带认证信息的URL会被拒绝。

该入口仅在准备新批次时覆盖原生LLM与Hybrid的LLM配置，并保存来源与配置哈希；纯Jev配置不变。第三方OpenAI兼容接口应选择现有的 `openai-compatible` 适配器。收到接口信息后仍需核对实际协议和供应商参数支持，不能仅凭接口名称假定兼容。

旧批次存在 `stop_requested.json` 且 `restart_authorized=false`。调度器及单槽位运行入口在触及账本或启动worker前拒绝恢复。更换提供者不自动授权恢复旧实验。

本次验证：禁网回归368项通过，用时5.33秒；测试入口为 `tests/jev`、`tests/test_replay_contract.py` 和 `tests/test_fairy_migration_smoke.py`。运行前替换 `socket.socket.connect`、`connect_ex` 与 `socket.create_connection` 为拒绝函数，测试环境仅使用离线占位凭据。未运行完整Agent或模型连通性探测。
