"""Versioned semantic task evaluation, preserving all Jev MVP v1 verdicts.

v2 removes two unsupported delivery-path restrictions: post-response soil
observations may come from returned sensor zones, and a native terminal assistant
message may be a final report. Neither model receives this evaluator's output.
"""
from __future__ import annotations

from copy import deepcopy
import math
import re

from fairy.agents.Jev.evaluation import evaluate_task as evaluate_v1

VERSION = "hn58_semantic_v2"


def returned_coverage(content, *, target_state=False):
    if not isinstance(content, dict):
        return set()
    covered=set()
    for row in content.get("ridges", content.get("ridges_overview", [content] if "ridge_id" in content else [])):
        if isinstance(row,dict) and type(row.get("ridge_id")) is int and ("growth_stage" in row or "irrigation_pending" in row):
            covered.add(row["ridge_id"])
    if not target_state:
        for row in content.get("soil_sensors", [content] if "vwc" in content else []):
            if not isinstance(row,dict) or type(row.get("vwc")) not in (int,float) or not math.isfinite(row["vwc"]):continue
            start,end=row.get("ridge_start"),row.get("ridge_end")
            if type(start) is int and type(end) is int and 0<=start<=end<=63:
                covered.update(range(start,end+1))
    return covered


def apply_semantic_contract(legacy, workflow, events=()):
    result=deepcopy(legacy)
    result["legacy_evaluation_v1"]={"task_success":legacy.get("task_success"),"assertions":deepcopy(legacy["assertions"])}
    result["evaluator_version"]=VERSION
    assertions=result["assertions"]
    target=set(assertions["new_irrigation_exact_target_and_dose"]["evidence"]["target_ridges"])
    from fairy.agents.Jev.tools import result_is_error
    steps=[s.to_dict() for s in workflow.dag.values() if s.tool_name]
    good=[s for s in steps if s.get("op_type")!="TOOL_ERROR" and not result_is_error(s.get("content"))]
    irrigation=[s for s in good if isinstance(s.get("content"),dict) and s["content"].get("status")=="irrigation_started"]
    first=min((s.get("time_before",math.inf) for s in irrigation),default=math.inf)
    pre=[s for s in good if s.get("time_after",math.inf)<=first]
    state=set().union(*(returned_coverage(s.get("content"),target_state=True) for s in pre))
    soil=set()
    for s in pre:
        content=s.get("content")
        if isinstance(content,dict) and ("soil_sensors" in content or "vwc" in content):soil.update(returned_coverage(content))
    quota=any(isinstance(s.get("content"),dict) and all(k in s["content"].get("management_regime",{}) for k in ("irrigation_quota_mm_total","irrigation_used_mm_total")) for s in pre)
    weather=any(isinstance(s.get("content"),dict) and all(k in s["content"] for k in ("temp_c","rainfall_mm","wind_speed_ms")) for s in pre)
    pre_evidence={"weather":weather,"soil":target<=soil,"target_state":target<=state,"quota":quota,"before_actual_irrigation":bool(irrigation),"source":"actual returned fields and timestamps; requested arguments alone do not prove coverage"}
    assertions["pre_irrigation_observations"]={"passed":all(pre_evidence[k] for k in ("weather","soil","target_state","quota","before_actual_irrigation")),"evidence":pre_evidence}
    physical=assertions["processed_waited_rechecked_response"]["evidence"]
    eta=physical.get("effect_ready_at")
    coverage=set(); rechecks=[]; completed_at=math.inf
    for step in good:
        if eta is None or step.get("time_before",0)<eta:continue
        observed=returned_coverage(step.get("content")) & target
        if observed:
            coverage.update(observed)
            rechecks.append({"step":step["name"],"tool":step["tool_name"],"returned_target_ridges":sorted(observed),"time_before":step.get("time_before")})
            if target<=coverage and math.isinf(completed_at):completed_at=step.get("time_after",math.inf)
    complete=bool(target) and target<=coverage
    physical.update(recheck_steps=[r["step"] for r in rechecks],semantic_rechecks=rechecks,returned_coverage=sorted(coverage),complete_recheck_at=None if math.isinf(completed_at) else completed_at)
    assertions["processed_waited_rechecked_response"]["passed"]=bool(physical["processed"] and physical["wait_steps"] and complete and physical["response_present"])
    reports=[]
    for step in good:
        if step["tool_name"]=="AgentUserInterface__send_message_to_user" and step.get("time_before",0)>=completed_at:
            reports.append({"source":"native_tool","step":step["name"],"content":str(step["tool_args"].get("content",""))})
    for event in events:
        data=event.get("native_event",{})
        if event.get("event")=="native_event" and data.get("event_type")=="llm_call_end" and data.get("status")=="ok" and not data.get("tool_calls") and event.get("sim_time_numeric",0)>=completed_at:
            reports.append({"source":"terminal_assistant","event_id":event["event_id"],"content":str(data.get("assistant_content") or "")})
    def fact_report(text):
        mentions=bool(re.search(r"灌溉|irrigat",text,re.I) and re.search(r"复查|recheck|检查|observ|moisture",text,re.I))
        # Preserve conservative templates as incomplete; keywords alone must not
        # turn an explicit admission of missing recheck into a completion report.
        denies=bool(re.search(r"尚未同时确认|不足以宣称.*完成|未完成.*复查|have not.*recheck|cannot confirm.*complet",text,re.I))
        return bool(text.strip()) and mentions and not denies
    accepted=[r for r in reports if fact_report(r["content"])]
    assertions["factual_final_report"]={"passed":bool(accepted) and assertions["new_irrigation_exact_target_and_dose"]["passed"] and assertions["processed_waited_rechecked_response"]["passed"],"evidence":{"reports":reports,"accepted_reports":accepted,"rule":"post-complete-recheck tool report or terminal assistant text; irrigation/recheck facts, no explicit incomplete-recheck disclaimer","limitations":"deterministic content checks, not a general semantic truth judge"}}
    result["task_success"]=all(a["passed"] for a in assertions.values()) if result.get("environment_check")=="passed" else None
    result["first_failed_assertion"]=next((k for k,v in assertions.items() if not v["passed"]),None)
    return result


def evaluate_task(scenario,engine,workflow,before,*,events=(),**kwargs):
    return apply_semantic_contract(evaluate_v1(scenario,engine,workflow,before,**kwargs),workflow,events)
