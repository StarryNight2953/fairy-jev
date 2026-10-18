# HN58 Tool Description v2 版本归档

运行提交：bbe762c98a97e5f22cceb5f7b24fc3a67656fdb6；冻结标签 refs/tags/experiment/hn58-tool-description-v2-20261019。

请先读 [report.md](report.md)。一组原生Qwen真实任务失败，两组Jev/Hybrid访问门禁阻塞；不是完成的三组真实对照。

完整原始证据：`/Users/ouyangkun/PHD/Project/Fairy-Jev/llm-jev-hybrid/runs/agent_comparison/hn58_tool_description_v2_20261019`。同级 `.tar.gz` 压缩包包含本轮全部live/scripted/模型请求响应、六份旧对照原字节副本和SHA256清单。大证据保存在runs，未全部提交Git。

本目录是C5时的说明及结果快照，原始结果以runs中的manifest/trace为准。orchestration/是已执行脚本的版本化副本，保留原脚本SHA对应关系；不要在这里执行live脚本或复用已消耗slot。没有再运行授权，也不会自动补测。旧证据内绝对路径保留，workspace-paths.json用于迁移解析。
