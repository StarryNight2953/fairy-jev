# GitHub branches and experimental versions

Private repository: `StarryNight2953/fairy-jev`.

| Reference | Purpose |
| --- | --- |
| `pure-jev` (default branch) | Current pure Jev implementation. Includes opt-in harvest-state and report-evidence policies, off by default. |
| `llm-jev-hybrid` (branch) | Current published LLM-Jev Hybrid snapshot. See that branch's documentation and configuration for its feature set. |
| `jev-l1-adapters-v2-20261020` (annotated tag) | Frozen source for the 78 L1 Jev tool-adapters-v2 experiment: `ac92e1ddd5d7be251e132258d5cbf139a3479263`. |

Long-lived branches identify implementations; tags identify frozen experimental versions. `jev-tool-state-contracts-v1` was renamed to `pure-jev`. The former `jev-tool-adapters-v2` branch is preserved by the annotated experiment tag above; its commit and history are unchanged. Subsequent development belongs on the appropriate implementation branch, not on the frozen experiment tag.

Historical experiment documents retain their original branch and filesystem names as provenance. Existing local worktree directories were not renamed. The experiment worktree remains at the same commit in detached HEAD mode.

This naming change does not modify Agent behavior, merge optional prototypes, launch experiments, or upload credentials and local datasets. Branch differences must be assessed from code and explicit configuration, not inferred from a shared repository.
