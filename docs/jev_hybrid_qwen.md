# 本地 Qwen 接入与 HN58 有界续跑

用户指定的本地模型已接入：`Qwen3.6-35B-A3B-FP8`，OpenAI-compatible 地址 `http://100.115.106.71:8000/v1`，复用 FAIRY 已有 `VLLMClient`。原生 LLM 与 hybrid updater 使用同一份配置；没有改动全局默认模型或升级 SDK。

实际原生 LLM smoke 和三个正式样本均打通模型→真实工具→反馈链路，`live_integration_success=true`；四次独立任务均为 `task_success=false`。Jev/hybrid 因已有 TypeSafe HTTP451 证据继续 blocked，任务及集成结果为 null。此次只执行了正式计划的3/9，另6个槽位保留阻塞证据，不能据此比较三种架构收益。

代码固定于 `4ad666c34abc2665425aef353cc38a9b2304d263`，标签 `experiment/hn58-three-agents-qwen-v1`；smoke、offline、formal 的 manifest 均为这一干净提交。旧实验标签及原始响应保持原样。报告之后的文档提交不是执行代码版本。完整逐次结果、版本与文件哈希见 [交付索引](experiments/hn58_three_agents_qwen_v1_results.json)。

配置为 `provider=vllm`、temperature0.1、max output2048、`parallel_tool_calls=true`、`enable_thinking=false`。thinking 设置沿用仓库 Qwen/vLLM 的现有默认，并显式写入有效配置；未搜索其他设置。`authentication_mode=none` 只允许明确指定地址的 vLLM，在内存副本中使用公开 SDK 占位值 `EMPTY`，不会继承环境里的真实 VLLM 密钥。已验证服务接受该请求。

| 修改位置 | 职责 |
|---|---|
| `fairy/experiments/credentials.py` | 统一凭据要求和无认证本地配置；原配置不原地修改 |
| `transports.py`、`episode.py` | 构造客户端与凭据预检复用上述规则；保留实际 HTTP 计数 |
| `access.py`、`agent_comparison.py` | 用当前目标配置匹配历史服务证据；旧 DeepSeek 缺 key 不迁移到 Qwen，原 TypeSafe451 继续生效 |
| `configs/jev/hn58_hybrid_sync_k4_qwen.json` | native/updater 共用 Qwen 参数、既有候选、K=4 和预算 |
| `configs/experiments/hn58_three_agents_qwen_v1.json` | 新批次身份、旧 smoke 证据引用和同一持久额度 |
| `tests/jev/test_local_qwen.py` 及 access/runner 测试 | 无真实密钥继承、原生 function calling、updater最多2HTTP/1逻辑更新、旧证据匹配 |

完整回归 **276 passed in 3.68s**。三组 scripted 各执行10次真实 FAIRY 模拟工具，五项断言、原生 validator、trace 和回放全部通过，模型 HTTP 为0。native/updater MockTransport 测试验证了实际 SDK 请求格式与有限修复；本次 hybrid 没有发起真实 updater 请求，不能把这些测试写成真实混合链路成功。

正式逐次结果如下。以下阻塞槽位为预定独立记录，不是失败任务样本；三次 native 都自然 `agent_finished`，没有更改模型动作、提示、候选或验收。

| 槽位 | 架构 | 集成 | 任务 | 状态 | HTTP | 工具 | 墙钟秒 |
|---|---|---|---|---|---:|---:|---:|
| formal_block_1_llm_native | llm_native | true | false | completed | 5 | 8 | 33.736 |
| formal_block_1_jev | jev | null | null | blocked | 0 | 0 | 预检，仅记录 |
| formal_block_1_hybrid | hybrid | null | null | blocked | 0 | 0 | 预检，仅记录 |
| formal_block_2_jev | jev | null | null | blocked | 0 | 0 | 预检，仅记录 |
| formal_block_2_hybrid | hybrid | null | null | blocked | 0 | 0 | 预检，仅记录 |
| formal_block_2_llm_native | llm_native | true | false | completed | 5 | 8 | 26.005 |
| formal_block_3_hybrid | hybrid | null | null | blocked | 0 | 0 | 预检，仅记录 |
| formal_block_3_llm_native | llm_native | true | false | completed | 3 | 5 | 13.134 |
| formal_block_3_jev | jev | null | null | blocked | 0 | 0 | 预检，仅记录 |

原生 LLM 正式集成3/3，任务0/3；这是三个预定小样本的描述，不是稳定成功率估计。三个失败样本全部纳入：总13次模型HTTP、21次工具，墙钟中位数26.005秒，范围13.134–33.736秒。各次输入／输出 tokens 为74,024／1,150，73,902／1,113，40,585／637；usage均由服务返回，费用未知。

独立五项断言在三次正式运行中相同：灌溉前观察 **false**、目标范围和累计剂量 **true**、配额消耗 **true**、等待后完整复查 **false**、合格最终报告 **false**。原生 validator 三次均为 true，单独保留，不能覆盖独立验收。所有真实 native 运行的 trace 校验和动作回放通过，无关键副作用差异。

首个可证实的问题三次都在 event43：读取天气、土壤和目标区后，尚未观察剩余配额便执行灌溉。模型实际选择43–63垄各0.8小时，范围和剂量正确；这是前置观察顺序／工具选择问题，不能归为参数候选或参数值错误。配额数据出现在灌溉之后的返回里，不能修复“操作前已观察”的要求。

区块1、2之后确实等待了120分钟，复查时刻晚于实际响应ETA，但仅查询S5（44–53）和S6（54–63），漏掉第43垄。区块3灌溉后直接给出最终回复，没有等待或复查；回复自身表示约2小时后才需复查。三次都有最终文本，报告断言失败不能写成“没有回复”。

区块1、2的最终文本还将配额称为“已用尽”，而真实返回为使用1.3125／总额1.4，仍余0.0875；同一回复另处又写出了余量。这个后续报告矛盾另行保留，不替代event43的首个问题，也不推断模型内部原因。

单列的 smoke 为5次HTTP、7次工具、34.411秒，输入61,532／输出1,150 tokens，集成true、任务false。首错为event38未先查询配额；等待有效，S5/S6仍漏43。原生validator为true，trace与7步动作回放通过。详见 [smoke分析](../runs/agent_comparison/local_qwen_preflight/smoke_analysis.json) 与 [正式分析](../runs/agent_comparison/local_qwen_preflight/formal_analysis.json)。

本次新增4个Qwen live episode、18次生成HTTP、28次真实模拟工具，服务累计报告输入250,043、输出4,050 tokens。另有一次 `/v1/models` 元数据GET，没有发起生成。连同原有一次TypeSafe451，整个任务累计 **5个live episode、19次模型HTTP**；同一持久账本未重置，剩余容量7个episode／1,421次模型HTTP不等于追加本轮已用样本的授权。原生LLM和Jev的smoke名额均已消耗，hybrid尚未实际执行。

旧交付索引曾记录当时活动账本的哈希；续跑前已将其原始字节保存为 [before快照](../runs/agent_comparison/local_qwen_preflight/live_budget_before_qwen.json)，哈希与旧索引一致。本次 [after快照](../runs/agent_comparison/local_qwen_preflight/live_budget_after_qwen.json) 固定新的消耗，活动账本继续使用原路径。原始run和旧实验标签没有改写。

状态：H0–H5通过，H6仅native真实链路通过、其余blocked，H7执行3/9并记录其余6个blocked，H8完成部分比较的诚实交付。实际命令、变更文件和证据在 [续跑检查点](../runs/agent_comparison/local_qwen_preflight/checkpoint.json)。

以下是本次实际执行的入口；smoke/formal的槽位与总预算不会通过重复命令重置。live进程使用用户授权的原env.sh，内容及密钥不写入日志。

```bash
cd /Users/ouyangkun/PHD/Project/Fairy-Jev-hybrid
.venv/bin/python -m fairy.experiments.agent_comparison --config configs/experiments/hn58_three_agents_qwen_v1.json --stage offline
set -a
source /Users/ouyangkun/PHD/Project/Fairy-Jev/env.sh
set +a
.venv/bin/python -m fairy.experiments.agent_comparison --config configs/experiments/hn58_three_agents_qwen_v1.json --stage smoke
# formal 在干净实验提交4ad666c及其标签上执行。
.venv/bin/python -m fairy.experiments.agent_comparison --config configs/experiments/hn58_three_agents_qwen_v1.json --stage formal
.venv/bin/python -m fairy.experiments.agent_comparison --config configs/experiments/hn58_three_agents_qwen_v1.json --stage report
```

当前本地LLM配置阻塞已解决，剩余真实混合验收仍受TypeSafe访问拒绝限制。没有新的真实混合材料片段，也无法比较混合整体延迟、成本或任务收益；不据此开展K搜索或自动修补模型行为。下一步需要先解决TypeSafe服务可用性，并保留本次native失败结果和同一累计预算。

交付前验证了204个本次run产物哈希，未发现不一致；原工作树31个白名单文件哈希保持不变。对1,385个新工作树文件按当前已加载API密钥值扫描，未发现匹配。检查只记录变量名与结果，不记录密钥或命中片段。
