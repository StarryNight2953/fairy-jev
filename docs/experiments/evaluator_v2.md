# HN58 semantic evaluator v2

This version was reviewed and tested offline before the three-architecture live
comparison. The Jev MVP evaluator remains unchanged. New evaluations save its
verdict in `legacy_evaluation_v1` alongside `hn58_semantic_v2`.

The public task asks for weather, soil moisture, target state and remaining quota
before irrigation, correct target/dose, waiting for response and rechecking.
The five requirements, quota calculation and physical-action assertions remain.
Three delivery-path corrections make this contract comparable across agents:

- Target-state coverage is obtained from returned ridge IDs and state fields,
  including `ridges_overview`; requested arguments alone are insufficient.
- Post-ETA response rechecks accept actual soil-sensor VWC coverage as well as
  returned per-ridge state. `SensorApp.read_soil_sensors` returns `ridge_start`,
  `ridge_end` and VWC and synchronizes physics. The public task does not mandate
  `get_ridge_range_state` after waiting.
- A final native assistant response after a completed recheck may deliver the
  report, as may the existing report tool. Native `Agent.run` normally terminates
  when its response has no tool calls; that message is not a Workflow tool node.

Evidence: `fairy/apps/farm_world/sensor_app.py` (read_soil_sensors),
`fairy/apps/farm_world/farm_world_app.py` (get_farm_overview/get_inventory),
`fairy/agents/agent/agent.py` (Agent.run), and the public task recorded in
`runs/agent_comparison/checkpoints/H0/contract.json`. Historical live
`20261017T112501_live_db897d9a` tool_0008 is a post-ETA soil observation covering
0–63. Its original v1 result is retained at the original source workspace.

The initial quota requirement is unchanged: farm overview omits management quota;
querying it after irrigation cannot repair a before-action obligation. Acceptance
uses returned quota/used fields, returned coverage and recorded timestamps.

Report checking is deterministic and limited: it requires irrigation and recheck
facts after complete recheck and rejects explicit incomplete-recheck disclaimers.
It is not a general natural-language truth judge. Conservative Jev reports remain
code-generated; native reports remain model-generated. This architectural
difference is recorded, not silently normalized away.

Validation: `tests/jev/test_semantic_evaluation.py` has 11 tests with real FAIRY
fixtures, including alternate valid paths, incomplete/early observations, false
argument coverage, native terminal text, conservative incomplete reports and
irreversible pre-quota failure. No evaluator output is supplied to either model.
