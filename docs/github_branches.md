# GitHub branches

Private repository: `StarryNight2953/fairy-jev`.

| Branch | Contents |
| --- | --- |
| `pure-jev` (default) | Current pure Jev branch, including opt-in harvest-state and report-evidence policies (off by default). |
| Tag `jev-l1-adapters-v2-20261020` | Frozen code used for the 78 L1 experiments: `ac92e1ddd5d7be251e132258d5cbf139a3479263`. |
| `llm-jev-hybrid` | Snapshot of the existing Hybrid working tree on 2026-10-21. |

This branch starts at `11bc28fc1854f85d9b7d69a9310d31c01be49db4` and preserves the 20 previously uncommitted code, documentation, and test files in the Hybrid working tree. Shared FAIRY, native LLM, and pure Jev components are retained. Publication does not change Agent behavior.

Optional observed-facts prototypes, tool adapters v2, and later state contracts from other worktrees have not been automatically merged. Compare code and configuration when evaluating differences between branches.

The Hybrid implementation is in `fairy/agents/Jev/hybrid/`. See [jev_hybrid.md](jev_hybrid.md) for implementation and configuration documentation; historical results there are not new validation of this snapshot.

This synchronization launches no model requests or experiments. Credentials, `env.sh`, ignored run logs, and local XLSX data are excluded. External model configuration and required datasets must be prepared locally according to the project documentation. This repository is a source-code archive, not the complete experiment-data archive.

## Naming and migration

Long-lived branches identify implementations; annotated tags identify frozen experimental versions. `jev-tool-state-contracts-v1` was renamed to `pure-jev`. The former `jev-tool-adapters-v2` branch is preserved by the annotated tag `jev-l1-adapters-v2-20261020`; its commit and history are unchanged. Historical experiment documents retain their original branch and filesystem names as provenance. Local worktree directories were not renamed.
