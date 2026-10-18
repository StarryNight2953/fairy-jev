# HN58 Tool Description v2：受控复测结果

日期：2026-10-19，Asia/Shanghai。Run ID 的时间前缀使用 UTC，因此 `20261018T165324` 对应本地 2026-10-19 00:53:24。

**本轮完成了版本冻结、离线验证、一个原生 Qwen 真实样本及两组访问阻塞记录。没有完成三组真实闭环，不能将 blocked 组记为模型任务失败。** 原生 Qwen 本次找到了配额查询，但结束前没有等待和复查，任务仍失败。未修改模型动作、未补测。

| 架构 | 交付状态 | runner状态 | live_integration_success | task_success | 前置观察 | 范围/剂量 | 配额消耗 | 等待/复查 | 最终报告 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| llm_native | failed | completed | true | false | true | true | true | false | false |
| jev | blocked | blocked | null | null | null | null | null | null | null |
| hybrid | blocked | blocked | null | null | null | null | null | null | null |

`failed` 是任务结果，原生循环正常返回 `agent_finished`。`null` 是没有执行验收，不是 false。独立 native validator 本次为 true，不替代五断言与 task_success。

## 唯一自变量与冻结身份

用户已经在三个 worktree 中作了相同修改。本轮使用 `llm-jev-hybrid` 的新实验分支 `experiment/hn58-tool-description-v2-20261019`，另外两树未被改写。生产修改单独提交为 `e868559`；第二次提交只添加五个离线传播测试。

运行提交：`bbe762c98a97e5f22cceb5f7b24fc3a67656fdb6`。冻结标签：`refs/tags/experiment/hn58-tool-description-v2-20261019`。交付文档后续提交不改变任何 run manifest 的实际执行提交。分支和标签同名，读取标签应使用完整 `refs/tags/`，避免 Git 的 ambiguous ref 警告；实验调用时两者都指向同一冻结提交。

旧说明：

> Return the current farm inventory (seeds, pesticide, fertilizer, fuel, grain).

用户新说明（原样使用，没有扩写）：

> Return the current farm inventory (seeds, pesticide, fertilizer, fuel, grain), management resource limits and usage (including irrigation quota), and post-harvest market information.

[单行 diff](/Users/ouyangkun/PHD/Project/Fairy-Jev/llm-jev-hybrid/runs/agent_comparison/hn58_tool_description_v2_20261019/user_docstring.diff)、[完整版本身份](/Users/ouyangkun/PHD/Project/Fairy-Jev/llm-jev-hybrid/runs/agent_comparison/hn58_tool_description_v2_20261019/c0_identity.json)、[实验 manifest](/Users/ouyangkun/PHD/Project/Fairy-Jev/llm-jev-hybrid/runs/agent_comparison/hn58_tool_description_v2_20261019/experiment_manifest.json)。

除该 Docstring 外，farm_world_app.py AST 完全一致，实际返回仍是原 inventory 字段加 management_regime、postharvest_market。Prompt、K=4、Choice instructions、FINISH、候选生成、参数集合、Agent Loop、报告模板、环境及 hn58_semantic_v2 未变。

新 catalog SHA（canonical visible entries）：`11315fbbc99523ada8dc88f256cec22d5e21a6840a8d164e22d216af7f0184f6`。58 个可见工具 + FINISH；工具 ID、可见性、顺序、参数 schema 均不变。原生 controller function schema、Jev 的实际 Choice 构造/SDK codec、Hybrid 的 PacketContext→LLMUpdater provider-bound JSON 均通过离线核验。三组 scripted 从相同 checkpoint 独立初始化，新 catalog 相同，10 个实际模拟工具各自完成，任务、trace、动作回放均通过，0模型HTTP。完整回归 **286 passed in 3.71s**。scripted 成功不计为真实模型成功。

## 配额行为与首次失败：直接证实

原生真实 run：`20261018T165324_llm_native_live_3b5f3748`。实际五工具顺序：天气 → 全部土壤传感器 → **get_inventory** → 43–63垄状态 → 对43–63垄灌溉0.8小时。随后返回终端文本，没有更多工具。

| 位置 | 原始证据与含义 |
| --- | --- |
| E7 / JSONL第8行 | 首个 HTTP request_body.tools 58工具中的 get_inventory 描述为新文本。三次真实请求均含相同新 schema。 |
| E8 / 第9行 | 第一原始模型响应选择 get_inventory，为四个并列 function calls 中第三个。 |
| E25 / 第26行 | 第3个实际工具返回 management_regime：irrigation_quota_mm_total=1.4，irrigation_used_mm_total=0。 |
| E35 / 第36行 | 下一真实请求 messages[5].content 对应该 tool_call_id，反序列化后与 E25 result 全对象完全相等；模型在灌溉前确实收到配额。 |
| E36 / 第37行 | 原始回复使用“总配额1.4、已用0、剩余1.4”，然后调用灌溉；本次没有将普通库存、overview 或灌溉日志冒充配额。 |
| E40→43 / 第41→44行 | 第5工具实际执行43–63垄、逐垄0.8小时，消耗全场等效1.3125mm，剩余0.0875mm。 |
| **E49 / 第50行** | 首个有直接证据的终止问题：原始HTTP响应没有tool_calls，finish_reason=stop；只把等待约2小时、复查S5/S6列为“下一步”。 |
| E50→52 / 第51→53行 | 原始响应进入SDK解析及原生llm_call_end；E52 dropped_tool_call_count=0，未丢弃工具调用。自动first_issue定位E52，原始决策来源是更早的E49。 |
| E55 / 第56行 | 原生循环agent_finished；不是超时、预算耗尽或工具执行错误。 |

证据：[原始 trace](/Users/ouyangkun/PHD/Project/Fairy-Jev/llm-jev-hybrid/runs/agent_comparison/hn58_tool_description_v2_20261019/live/20261018T165324_llm_native_live_3b5f3748/trace.jsonl)；[配额决策完整窗口](/Users/ouyangkun/PHD/Project/Fairy-Jev/llm-jev-hybrid/runs/agent_comparison/hn58_tool_description_v2_20261019/analysis/native_quota_window.json)；[终止完整窗口](/Users/ouyangkun/PHD/Project/Fairy-Jev/llm-jev-hybrid/runs/agent_comparison/hn58_tool_description_v2_20261019/analysis/native_termination_window.json)；[完整实际模型请求/响应](/Users/ouyangkun/PHD/Project/Fairy-Jev/llm-jev-hybrid/runs/agent_comparison/hn58_tool_description_v2_20261019/analysis/llm_native_model_calls.json)。窗口保存原事件及源SHA/行号，不重建模型输出。

最后文字明确存在，包含实际范围、时长、用水量、剩余配额，并写“下一步：等待约2小时……复查 S5/S6”。这不是已执行等待/复查，也没有声称已复查。工具反馈 ETA 为1786067530.4392343；episode结束仿真时间1786060337.769911，尚未到ETA。没有wait步骤，没有recheck步骤。

验收中的 reports=[] 是因只接受完整复查之后的报告，**不能解释为没有最终文本**。公开任务明确要求“等待响应后复查”，本次不满足这个公开要求，因此任务未完成的结论不依赖报告评分争议。公开任务没有精确给出growth_stage/irrigation_pending等代理字段或最终报告完整词汇；保留现有验收规则和该限制，没有放宽。

模型未来计划只查S5/S6（覆盖44–63），若照此执行会有漏43垄风险；它没有执行，不能写成已经发生的复查覆盖错误。E36也将S4概括为其他分区，但S4实际上跨33–43；全部土壤传感器真实返回已经覆盖43–63，不能因此否定本次已通过的前置土壤观察。

## 访问阻塞和真实传播边界

Jev和Hybrid的本轮run均停于 `preflight_blocked`，没有创建TypeSafe后端、没有任何HTTP、没有工具尝试、不占用live episode。对应输出含manifest/catalog/trace/workflow/evaluation/metrics/verification；动作回放标为不适用，因为没有执行工具。不能把它们的0次调用计为快速完成，也没有本轮Jev响应模型可报告。

未修改的service_blockers_from_smoke合并了旧v1、qwen-v1 smoke索引和迁移后真实Hybrid manifest派生记录。它仍将旧Jev `20261018T094403_jev_live_3ec48d66` E6 的HTTP451应用于同服务身份的两架构；后来的代理成功记录不会自动清除此block。这里是**现有门禁的历史证据阻塞，不是本日又测得451**。不筛掉历史拒绝、不换出口、不改gate、不发送“确认可用”探测。[完整门禁结果](/Users/ouyangkun/PHD/Project/Fairy-Jev/llm-jev-hybrid/runs/agent_comparison/hn58_tool_description_v2_20261019/access_review.json)。

离线已确认三组描述传播；真实HTTP中仅native已核验。纯Jev实际Choice、Hybrid实际updater/Jev请求、材料是否仍写“日志=配额”、空日志后是否提前灌溉，本轮均**无法观察**。旧Hybrid仍为integration=true/task=false，本轮不覆盖旧样本。

## 实际请求、延迟和用量

| 架构 | 实际逻辑模型请求 / HTTP | 工具尝试 | 完整episode秒 | 模型HTTP累计秒 | 输入 / 输出tokens |
| --- | --- | --- | --- | --- | --- |
| llm_native | 3 / 3 | 5（全部成功） | 26.402900 | 25.482436 | 35,772 / 645 |
| jev | 0 / 0 | 0 | 0.373291（仅阻塞检查） | 不适用 | unknown / unknown |
| hybrid | 0 / 0 | 0 | 0.308770（仅阻塞检查） | 不适用 | unknown / unknown |

原生三次HTTP均200、retry index=0；实际无重试，不能说禁用了原生重试。服务返回模型均为Qwen3.6-35B-A3B-FP8，fingerprint均为vllm-0.22.0-bbe13ed8。输入/输出token为三次请求累计服务usage，不是单次上下文长度。三次延迟分别6.716538、11.463239、7.302659秒；输入token分别8887、13063、13822；输出128、321、196。完整usage在model_calls导出中；首请求cached token缺失不能按0计。

费用均unknown，未假定单价或缓存折扣。原生使用现有vLLM endpoint `http://100.115.106.71:8000/v1/chat/completions`；离线构造未修改SDK时选择系统代理127.0.0.1:7897。记录的是SDK路由选择与实际HTTP endpoint/status，不是网络抓包证明的物理路径。没有更改代理策略。[路由及SDK版本记录](/Users/ouyangkun/PHD/Project/Fairy-Jev/llm-jev-hybrid/runs/agent_comparison/hn58_tool_description_v2_20261019/runtime_preflight.json)。

## 与旧结果对照：事实、可能解释及不能确认

| 对照 | 旧原生 seed1904 | 新原生 seed1904 |
| --- | --- | --- |
| 五断言（前置/剂量/消耗/复查/报告） | false,true,true,false,false | true,true,true,false,false |
| 灌溉前配额 | 未查；farm overview只有普通库存 | inventory真实管理配额，决策前收到 |
| 等待/复查 | 等120分钟，查S5和S6，漏43垄 | 完全未执行等待/复查，只给未来计划 |
| 最后文字 | 有，复合报告断言失败 | 有，复合报告断言失败 |
| HTTP / 工具 | 5 / 8 | 3 / 5 |
| 完整episode秒 | 33.735509 | 26.402900 |
| 输入 / 输出tokens | 74,024 / 1,150 | 35,772 / 645 |
| task_success | false | false |

**直接证实**：与旧seed1904样本相比，首个真实HTTP body除inventory.description外完全一致（初始messages、58工具schema/顺序、generation字段）；本次发现并读取配额，前置观察从false变true。旧原生另外两个seed1905/1906也漏事前配额，新样本只有seed1904，不是三次重复。

旧原生执行提交4ad666c，新提交bbe762c；配置hash相同（`356f2bcd0039fbee4edae8ce4509ca102c89fd0147bd203612be5a3626636d46`），同模型/SDK、checkpoint、初始仿真时间、场景源文件、数据workbook、prompt/candidate/evaluator/lockfile hash相同。从旧提交到新提交的其他runtime变化是先前已存在的路径迁移适配，本轮未再改动。原始HTTP首请求严格对照及各旧样本的配置差异完整保存在[comparison.json](/Users/ouyangkun/PHD/Project/Fairy-Jev/llm-jev-hybrid/runs/agent_comparison/hn58_tool_description_v2_20261019/comparison.json)。

旧Hybrid是a9df0e5诊断smoke，6工具/11HTTP，五断言false,true,true,true,false；本轮blocked不能比较描述后的行为变化。更早纯Jev记录是基于215f8c3的未提交MVP实现，40工具、69次Choice、max_steps，使用较早未版本化验收；manifest实现哈希而非基准commit单独描述代码，不能当作当前冻结实现的严格配对对照。三架构批次中的纯Jev451记录没有有效闭环。六份旧对照的manifest/trace/evaluation等已按原字节复制到本包old_controls/；原文件保持不动。

**可能解释**：更明确的quota能力说明帮助Qwen发现正确查询；与观测到的行为变化一致，但一次样本无法证明唯一因果。

**无法确认**：Jev/Hybrid新版的任务表现、稳定成功率提升、统计显著性、总体架构优劣或效率收益。场景seed相同不等于远端采样确定；没有指定remote sampling seed，temperature=0.1，墙钟推进仍造成传感器观测变化。新版少做必要步骤，所以请求/延迟更低不能叫效率改善。未把这些限制转化为额外补样理由。

## 检查点、预算与版本化交付

| 检查点 | 状态 | 核心证据 |
| --- | --- | --- |
| C0 | PASSED | c0_snapshot.json / c0_identity.json / preregistration.json：三树、原始修改、冻结版本、同一账本 |
| C1 | PASSED | c1_result.json / offline_propagation/：唯一描述差异、三接口离线传播 |
| C2 | PASSED | regression.log / c2_scripted_result.json：286测试、三scripted、0模型HTTP |
| C3 | PARTIAL_ACCESS_BLOCKED | result_llm_native.json / result_jev.json / result_hybrid.json：一次真实运行、两门禁阻塞，无补测 |
| C4 | COMPLETE_WITH_BLOCKED_ROWS | comparison.json / analysis/ / evidence_index.json：原始响应、五断言、首次错误、旧样本与限制 |
| C5 | COMPLETE | experiment_manifest.json / report.md / budget_after.json / preservation.json / files.sha256及版本归档 |

完整命令、变更与检查结果见[commands.json](/Users/ouyangkun/PHD/Project/Fairy-Jev/llm-jev-hybrid/runs/agent_comparison/hn58_tool_description_v2_20261019/commands.json)；离线阶段说明见[preflight_report.md](/Users/ouyangkun/PHD/Project/Fairy-Jev/llm-jev-hybrid/runs/agent_comparison/hn58_tool_description_v2_20261019/preflight_report.md)。本次唯一生产diff是用户Docstring；新增测试和归档脚本不进入Agent决策逻辑。全部原始材料SHA见[evidence_index.json](/Users/ouyangkun/PHD/Project/Fairy-Jev/llm-jev-hybrid/runs/agent_comparison/hn58_tool_description_v2_20261019/evidence_index.json)和files.sha256。

同一ledger ID ba456f29add04993a04ac60636f1dc0f从6 episodes/30HTTP到**7/12 episodes、33/1440HTTP**，本轮新增1episode/3HTTP，剩余5episode/1407HTTP；这是只读记录，不授权补测。前三架构按各自配置和相同seed1904独立初始化，没有跨Agent注入观察。没有重建账本、消费旧slot、修改旧失败结果或放宽验收。

保全检查在归档前重新核对927个原有文件及另两树HEAD/status，见preservation.json。新原生5步动作回放通过，复现任务失败；blocked组没有动作回放。原生真实轨迹原样保留，没有追加人工动作。

阅读入口：本report → comparison.json → analysis/*window.json或各run/trace.jsonl。`live/` 保存本轮原始证据；`scripted/`保存离线闭环；`old_controls/`为旧证据原字节副本；`offline_propagation/`为离线接口材料。回放和verification都只验证各自声明的范围，不能将完整性通过解释为任务成功。
