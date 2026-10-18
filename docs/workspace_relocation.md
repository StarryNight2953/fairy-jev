# 工作区迁移与混合版本复测

2026-10-18，按用户要求将三个工作树集中到 `/Users/ouyangkun/PHD/Project/Fairy-Jev/`：`jev-mvp` 为原工作区，`llm-jev-hybrid` 为开发工作树，`hn58-reproduce` 为冻结复现工作树。父目录的 `Fairy-Jev.code-workspace` 可一次打开三者。

目录迁移后的 HEAD、原有 Git status 和 3,220 个源码/证据文件哈希与迁移前一致。随后仅修复虚拟环境生成文件中的路径；三套解释器从 `/tmp` 导入均指向自己的新目录，依赖版本未变。原始凭据文件未复制，历史 trace、manifest、workflow 和失败结果未改写。

`fairy/experiments/paths.py` 读取父目录 `workspace-paths.json`，将已保存的旧绝对路径解析到新位置。预算账本、历史 access 证据和比较报告通过该映射读取；映射目标缺失会报错，不能以新建空账本重置实验消耗。当前配置与历史配置中的原路径均可解析。新建相对路径和已迁移路径保持原意。

测试前预算仍为 ledger `ba456f29add04993a04ac60636f1dc0f` 的 5 个 live episode、19 次模型 HTTP；hybrid smoke 尚未使用。新增路径回归后共 **281 passed**。迁移后 scripted hybrid 完成 10 次真实 FAIRY 模拟工具，任务验收通过，trace 与动作回放结果保存在独立测试目录。

本次真实测试使用唯一剩余 `smoke_hybrid` 名额，模型仍为用户指定的本地 `Qwen3.6-35B-A3B-FP8` 与官方 SDK `jev-1.13.0`；K=4、候选、提示和任务验收保持原配置。上限保持 600 秒、40 工具、40 轮、80 次 Choice、120 次 HTTP；每次 updater 最多两次实际请求。它是迁移后的诊断 smoke，不追加或替换旧冻结批次中的正式样本。

此前 HTTP 451 来自 `85ac64f` 显式无代理 transport。当前 SDK 默认 transport 已保留系统代理；本次只读检查实际选择 `127.0.0.1:7897`，与旧调用条件不同。历史 access 拒绝证据仍保留，通用 access gate 未放宽。本次按用户新的测试请求单独核对当前链路，不事先宣称 451 已解决；若仍失败，保留原始响应并停止，不再另建 smoke 名额。

证据目录：`runs/agent_comparison/hybrid_relocated_20261018/`。迁移和虚拟环境修复记录位于父目录 `workspace-evidence/20261018T215018/`。测试结果将在实际完成后追加。
