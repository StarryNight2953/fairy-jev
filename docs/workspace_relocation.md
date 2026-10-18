# 工作区迁移与混合版本复测

2026-10-18，按用户要求将三个工作树集中到 `/Users/ouyangkun/PHD/Project/Fairy-Jev/`：`jev-mvp` 为原工作区，`llm-jev-hybrid` 为开发工作树，`hn58-reproduce` 为冻结复现工作树。父目录的 `Fairy-Jev.code-workspace` 可一次打开三者。

目录迁移后的 HEAD、原有 Git status 和 3,220 个源码/证据文件哈希与迁移前一致。随后仅修复虚拟环境生成文件中的路径；三套解释器从 `/tmp` 导入均指向自己的新目录，依赖版本未变。原始凭据文件未复制，历史 trace、manifest、workflow 和失败结果未改写。

`fairy/experiments/paths.py` 读取父目录 `workspace-paths.json`，将已保存的旧绝对路径解析到新位置。预算账本、历史 access 证据和比较报告通过该映射读取；映射目标缺失会报错，不能以新建空账本重置实验消耗。当前配置与历史配置中的原路径均可解析。新建相对路径和已迁移路径保持原意。

测试前预算仍为 ledger `ba456f29add04993a04ac60636f1dc0f` 的 5 个 live episode、19 次模型 HTTP；hybrid smoke 尚未使用。新增路径回归后共 **281 passed**。迁移后 scripted hybrid 完成 10 次真实 FAIRY 模拟工具，任务验收通过，trace 与动作回放结果保存在独立测试目录。

本次真实测试使用唯一剩余 `smoke_hybrid` 名额，模型仍为用户指定的本地 `Qwen3.6-35B-A3B-FP8` 与官方 SDK `jev-1.13.0`；K=4、候选、提示和任务验收保持原配置。上限保持 600 秒、40 工具、40 轮、80 次 Choice、120 次 HTTP；每次 updater 最多两次实际请求。它是迁移后的诊断 smoke，不追加或替换旧冻结批次中的正式样本。

此前 HTTP 451 来自 `85ac64f` 显式无代理 transport。当前 SDK 默认 transport 已保留系统代理；本次只读检查实际选择 `127.0.0.1:7897`，与旧调用条件不同。历史 access 拒绝证据仍保留，通用 access gate 未放宽。本次按用户新的测试请求单独核对当前链路，不事先宣称 451 已解决；若仍失败，保留原始响应并停止，不再另建 smoke 名额。

证据目录：`runs/agent_comparison/hybrid_relocated_20261018/`。迁移和虚拟环境修复记录位于父目录 `workspace-evidence/20261018T215018/`。父目录的 `workspace-paths.json` 必须随工作区保存，不能丢弃后继续使用含历史绝对路径的配置。

## 实际测试结果

真实运行代码为干净提交 `a9df0e524a60b4d35db7c40a8a7c497f62b62576`，run 为 `20261018T140025_hybrid_live_9fd09d81`。本报告后续提交不改变 manifest 中的实际执行版本。

| 检查 | 实际结果 |
| --- | --- |
| 回归 | 281 passed in 3.48s |
| 同提交 scripted | 10 次真实模拟工具；任务、trace、回放均通过；0 HTTP |
| `live_integration_success` | **true** |
| `task_success` | **false** |
| 原生 validator | true，单独保留 |
| 终止 | Jev 选择 FINISH，`model_finish`，没有人工补动作 |
| 实际请求 | 11 HTTP：Qwen updater 2、Jev 工具 Choice 7、参数 Choice 2；全部 200，无重试 |
| 实际工具 | 6 次，全部执行成功 |
| 时间 | 22.841 秒完整 episode；Qwen HTTP 16.455 秒，Jev HTTP 共 5.589 秒 |
| tokens / 费用 | 服务报告输入 123,107、输出 6,595；费用未知 |
| 证据验证 | trace 通过；6 步真实工具回放通过，关键副作用无差异 |
| 同一累计预算 | 6/12 live episode，30/1,440 HTTP；新增仅此次 hybrid smoke |

本次通过系统代理成功调用 TypeSafe，451 未再出现。这说明当前调用链路可用，不能据一次复测断言所有地区/出口均可用，也没有改写旧 451 结果。

实际动作依次为：读取天气 → 读取全部土壤传感器 → 读取灌溉日志 → 对43–63垄各灌溉0.8小时 → 推进时间10,800秒 → 再读全部土壤传感器 → FINISH。目标及剂量、实际配额消耗、等待后的完整复查这三项断言通过；灌溉前观察和最终报告两项失败。

首个材料问题可见于 event14 的 `packet_0001`：计划遗漏目标区状态观察，并建议通过 `FieldOpsApp__get_irrigation_log` 检查日志/配额。该工具在 event49 实际仅返回 `{"irrigation_log": []}`。Jev 随后直接选择灌溉，event68 构成首个不可逆任务违约：尚未观察目标区状态和剩余配额。完整菜单中仍包含相关观察工具；候选正确表达并执行了43–63垄、0.8小时，因此不能将此归为参数候选不足或参数选择错误。

第4次工具之后真实 Qwen 生成第二版材料，event81 接受 `packet_0002`，其引用的原始证据为25、37、49、68。该材料把前置条件称为已完成，并用已成功灌溉推断此前配额可用；它未识别任务要求的“操作前已观察”。不过其等待后复查建议被执行：Jev 等待3小时，超过可见2小时ETA，再读取全部传感器，覆盖包含第43垄在内的完整目标区。event119 直接 FINISH，未发送最终事实报告。

真实跨K机制已逐事件核验：材料在工具计数0和4生成；第二版原样进入实际 Jev HTTP 请求 events86、94、105、117，均返回200。第二版支持两次工具动作和最终FINISH；第一次等待结果 event99、复查结果 event111 在没有第三次 LLM 更新的情况下进入后续请求的新反馈。两个参数 Choice 与各自工具 Choice 使用同一冻结材料快照。这里证明了混合闭环实际工作，不代表模型计划和任务完成判断正确。

原始 [trace](../runs/agent_comparison/hybrid_relocated_20261018/smoke/20261018T140025_hybrid_live_9fd09d81/trace.jsonl)、[evaluation](../runs/agent_comparison/hybrid_relocated_20261018/smoke/20261018T140025_hybrid_live_9fd09d81/evaluation.json)、[复测摘要](../runs/agent_comparison/hybrid_relocated_20261018/smoke_result.json) 和 [独立材料分析](../runs/agent_comparison/hybrid_relocated_20261018/agent_analysis.json) 保留全部模型动作与结果。本次没有改变提示、候选、验收，也没有重跑原生/纯Jev的已用smoke或修改旧正式批次；该单次诊断不构成完整三组收益对照。

实际执行入口（在 `llm-jev-hybrid` 中）：

```bash
DEEPSEEK_API_KEY=offline-regression-placeholder DASHSCOPE_API_KEY=offline-regression-placeholder .venv/bin/python -m pytest -q tests/jev tests/test_replay_contract.py tests/test_fairy_migration_smoke.py
.venv/bin/python -m fairy.agents.Jev --architecture hybrid --mode scripted --config configs/jev/hn58_hybrid_sync_k4_qwen.json --output-dir runs/agent_comparison/hybrid_relocated_20261018/scripted_frozen
set +x
set -a
source ../jev-mvp/env.sh
set +a
.venv/bin/python runs/agent_comparison/hybrid_relocated_20261018/run_smoke.py
```

最后一条命令为本次已执行记录；脚本与账本均阻止再次消费同一smoke，不能作为无限复测命令。它在调用前写入 preregistration，随后调用既有 `run_episode` 和 `verify_and_replay`。源码迁移修复、真实调用与报告分别记录；大型原始证据未批量提交。
