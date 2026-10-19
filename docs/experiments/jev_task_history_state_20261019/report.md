# 纯Jev第二策略：任务＋完整调用历史

**已新增 `task_history_v1`，原 `visible_state_v1` 保留为默认。首次有界真实对照中，原输入再次触发上下文超限，新输入被服务接受并返回有效Choice。** 这证明新策略可以避开这个已保存的失败点；它不能保证整个场景完成，也不能普遍解决上下文增长。

实现提交：`27a04823959505aa8d817f02bc6fa1a73279e055`。该版本在工作树干净时完成scripted闭环及真实冻结请求诊断。代码基线为此前修复提交 `2e03d0d4f7b57a84dbf16dd5013548ee455e5b1e`。本报告所在后续提交只归档结果说明。

| 选择 | 模型实际接收的状态 | 保留情况 |
| --- | --- | --- |
| `visible_state_v1` | 原有最新工具观察、近期动作、写操作/错误事实等 | 原实现和默认选择保留 |
| `task_history_v1` | 初始任务＋按顺序排列的全部调用名、参数、完整结果 | 新增，显式选择才启用 |

新策略还保留当前时间、动作前后时间、通知顺序、执行状态、错误详情、系统上下文及控制器反馈。这些是相对参考项目最简state的明确增强，避免重引入其丢失通知和时间的问题。结果不裁剪，不做摘要，不删掉重复查询或重复报告。

仅改变纯Jev的模型输入投影。全量工具菜单、完整参数组合候选、两阶段Choice指令、执行循环、报告模板及验收规则不变；候选生成继续读取原来的可见状态。同轮工具与参数选择使用同一份冻结投影。Hybrid保持原来的packet/fresh-events机制，并拒绝误用该纯Jev策略。

可通过新配置 `configs/jev/hn58_task_history.json` 启用，或给原Jev CLI添加 `--state-strategy task_history_v1`。例如：

```sh
PYTHONPATH=. .venv/bin/python -m fairy.agents.Jev \
  --mode scripted --config configs/jev/hn58_task_history.json \
  --output-dir runs/jev/task_history_scripted
```

省略新选项仍使用旧方式；`--state-strategy visible_state_v1` 可显式切回。详见 [输入合同及使用说明](../../jev_task_history_strategy.md)。

真实对照选取停跑批次中**按时间最早**的纯Jev超限样本，未按缩减幅度择优：`20261019T083659_jev_live_d254aa4d`，场景 `scenario_l1_hb_coldspring_heihe50_harvest_store_ready`。冻结历史E341 / `http_0039`，之前已有27个工具动作；历史E342为400超限。A保留原请求，B只替换state，问题、工具选项、候选顺序、模型与请求参数完全相同。实际SDK发送前再次核验请求相等性。

| 冻结请求组 | SDK重建的完整请求字节 | 本轮真实HTTP结果 | HTTP耗时 | 服务报告用量 |
| --- | ---: | --- | ---: | --- |
| A 原state | 72,022 | 400，`max_tokens_exceeded` | 1.220秒 | 未报告 |
| B `task_history_v1` | 62,188 | 200，有效Choice：`TractorApp__unload_grain` | 0.544秒 | 输入27,972 / 输出740 tokens |

B较A减少13.65%的请求字节。响应模型为 `jev-1.13.0`，官方SDK0.7.5；原始响应、概率、confidence、请求ID和usage均保留。这里没有验证所选卸粮动作是否正确，**也没有执行它**。这不是完整Agent运行，不等于通过该收获任务；单次延迟差也不能推广为性能改善。

A、B各一次，总共2次HTTP，无重试、无追加样本。诊断请求沿用TypeSafe的30秒超时，整体上限120秒，不改变已配置的LLM1800秒和新完整episode7200秒时限。没有调用Qwen。开始时两次本地准备发现配置文件迁移及变量未export，均为0 HTTP、0账本槽位；随后用已获授权的 `../jev-mvp/env.sh` 配合 `set -a` 导出环境，才执行唯一的A/B请求对。所有准备记录保留，密钥未写入日志或聊天。

同时完成了全部历史失败点的离线对照：停跑批次120个终态纯Jev记录中，提取62个首次 `max_tokens_exceeded` 请求；其中60个工具Choice、2个参数Choice。只改变state，保留相同历史前缀和原始任务，包括3个历史错误任务输入。一个中断且缺manifest的记录单独排除，不伪造终态结果。

| 投影 | 请求缩小 / 增大 | 配对字节变化中位数 | 62个请求字节总量变化 |
| --- | ---: | ---: | ---: |
| 参考项目最简task＋action/arguments/result | 52 / 10 | −17.33% | +2.52% |
| 新实现，另保留时间、执行状态及可见反馈 | 52 / 10 | −13.29% | +9.37% |

最明显反例为 `20261019T105914_jev_live_d83b0a91`：此前139次调用，包含46次范围状态查询、16次总览和74次时间推进。旧策略仅保留部分最新读取；全历史带回全部读取后，请求由75,089增至524,620字节，**增加598.66%**。另一个样本171次调用中有156次用户通信，新请求由74,652增至180,007字节。

另检查较早HN58补充实验 `20261018T173848_jev_live_a0df2d85` 的17个请求，全部缩小，总量减少10.93%。该HN58样本原本没有上下文超限，且本轮没有为HN58运行真实Agent，所以不能把该结果当作HN58超限已解决。

所有离线尺寸都使用安装的SDK序列化完整JSON，包含state、问题和菜单。旧trace保存解析后的请求JSON，因此称为固定SDK编码的重建尺寸；不是原始网络字节计数，更不是token估计。失败响应未提供tokens，不能用前一次成功请求的tokens冒充失败或反事实请求用量。离线扫描选中的79个前缀没有环境通知/控制器反馈；通知保留由专门离线测试验证。

验证结果：

- **730项离线测试通过**，网络连接被禁止。覆盖默认投影保留、全历史/错误/空值/通知顺序、超过百万字符不裁剪、深拷贝、两阶段相同输入、58工具候选不变、真实SDK冻结请求和预算边界。
- HN58新策略scripted闭环：10次真实本地工具调用，0模型请求，`scripted_task_success=true`，trace校验和动作回放均通过。它证明接入与执行路径正常，不是模型能力结果。
- 旧停跑批次6,706文件校验通过；另外两个工作树与参考源文件保持原样。原停止状态未解除。
- 同一账本 `ba456f29add04993a04ac60636f1dc0f` 新增1个诊断槽位、2次HTTP；累计368槽位、30,854次HTTP。历史行和上限保留，没有重置预算。

本轮结果应分别解释：`frozen_request_acceptance` 为A失败、B通过；`scripted_task_success=true`；完整真实episode的 **`live_integration_success=null`、`task_success=null`**，因为没有运行完整真实Agent。脚本正常结束与任务通过仍分别记录。

直接证据支持“这一组织方式可以减少部分前缀的输入开销，并暂时避开一个真实超限点”。当前旧策略已对读取和报告做过保留范围限制，因此全历史会在部分长轨迹上更大。它值得作为第二实现继续对照，但不适合据此直接替换默认实现。是否能减少完整运行中的超限频率、延后多少步、是否改善动作质量与任务完成，仍未得到本次实验验证。

复核入口：

- [机器可读结果](summary.json)。
- [完整证据目录](../../../runs/diagnostics/jev_task_history_state_20261019/)。
- `live_probe_exported_env/`：唯一真实A/B的预注册、完整请求、逐组原始结果、trace及预算前后快照。
- `results/failure_comparisons.json`、`failure_requests/`：62个超限点的原、新和参考最简请求；`results/hn58_comparisons.json`、`hn58_requests/`：17个历史HN58请求。
- `audit.py`、`command_record.json`、`results/component_findings.json`：离线重构方法、命令及输入构成。
- `scripted/`、`scripted_summary.json`、`offline_regression.log`：本地闭环、回放和测试证据。
- `execution_identity.json`、`implementation_plan.json`、`preservation_check.json`、`delivery.json`、`files.sha256`：版本、计划、保全与交付校验。
