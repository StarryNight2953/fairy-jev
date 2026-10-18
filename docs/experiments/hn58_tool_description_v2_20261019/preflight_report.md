# Tool Description v2：C0–C2 实验前检查

检查日期：2026-10-19（Asia/Shanghai）。本次只检验用户现有 get_inventory Docstring 的最小更新。

| 检查点 | 结果 | 命令与证据 |
| --- | --- | --- |
| C0 | 通过版本及预算检查；访问资格分别记录 | `git worktree list --porcelain`、三树 `git status --short` / `git rev-parse HEAD` / `git diff -- fairy/apps/farm_world/farm_world_app.py`；见 c0_snapshot.json、c0_identity.json、user_docstring.diff |
| C1 | 通过：新目录只有一个 description 字段变化 | `.venv/bin/python runs/agent_comparison/hn58_tool_description_v2_20261019/offline_checks.py catalog`；见 c1_result.json、offline_propagation/ |
| C2 | 通过：286 passed in 3.71s；三组 scripted 验收、trace及回放均通过 | `.venv/bin/python runs/agent_comparison/hn58_tool_description_v2_20261019/run_regression.py`；`offline_checks.py scripted`；见 regression.log、c2_scripted_result.json、scripted_index.json |

冻结运行提交：`bbe762c98a97e5f22cceb5f7b24fc3a67656fdb6`；新标签 `experiment/hn58-tool-description-v2-20261019`。生产变更提交 e868559 仅改变一行 Docstring；第二次提交只增加 tests/jev/test_tool_description_v2.py。新分支位于原 llm-jev-hybrid 工作树，没有改写另两树或移动旧标签。目录中所有后续生成材料均属于本独立新批次。

新 catalog：`11315fbbc99523ada8dc88f256cec22d5e21a6840a8d164e22d216af7f0184f6`。58 个可见工具、59 个 Choice 选项及顺序、参数 schema、可见性均与旧 hybrid 样本一致；除 Docstring 外 farm_world_app.py 的 AST 相同。直接检查了原生 controller schema、OfficialBackend.decide 的公共 Choice 构造/SDK codec、PacketContext→LLMUpdater 实际消息构造路径。这里是离线接口核验；真实发送体只在 C3 已实际发送的架构中核验，blocked 组不能宣称已发送。

新增测试首次局部运行 4 passed / 1 failed：测试误把 Docstring 中的普通库存词当作真实返回键。仅修正新增测试为既有 InventoryState.to_dict 的七个实际字段，随后5项全部通过；没有修改生产返回结构或 Agent。完整回归包含这5项，不依赖真实凭据，socket连接被拒绝。三组 scripted 都使用冻结提交、新 catalog、seed1904、相同 checkpoint clock，分别独立初始化，均0 HTTP；其成功不计入真实模型成功率。

统一 ledger ID `ba456f29add04993a04ac60636f1dc0f`，C0 余额6/12 episodes、30/1440 HTTP；无 reserved/running。历史两份预登记均无未完成占位。现有 ledger 经 fairy/experiments/paths.py 解析，不创建替代账本。三组已预登记唯一新 slot，各上限40工具/40轮/120HTTP/600秒，保留既有更严格限制；新批次最多3个episode、360HTTP。C3每次重新核对冻结身份、门禁及账本。

访问检查：未修改的 service_blockers_from_smoke 合并旧两个 smoke 索引及迁移后真实 hybrid manifest 派生记录，native=[]，Jev/Hybrid各有旧451阻塞。迁移后 hybrid 的成功证据一并提供，现有门禁没有成功覆盖历史拒绝或按代理区分身份的规则，因此不自行解除。见 access_review.json。此结论表示当前软件门禁阻塞，不等于本日 TypeSafe 实测仍返回451。

凭据只检查存在性，不保存值。C0客户端只读路由检查显示原生Qwen的未修改SDK选择127.0.0.1:7897系统代理，未进行连接探测。TypeSafe因门禁没有构造SDK客户端。见 runtime_preflight.json。没有更换模型、代理、温度、输出预算、Prompt、K=4、候选、Loop、FINISH、报告模板或hn58_semantic_v2评价器。

实际执行前准备命令（凭据source输出被抑制，未打印值）：`set +x; set -a; source ../jev-mvp/env.sh >/dev/null 2>&1; set +a`，随后运行 `.venv/bin/python runs/agent_comparison/hn58_tool_description_v2_20261019/run_registered.py prepare`。原生Qwen为authentication_mode=none。
