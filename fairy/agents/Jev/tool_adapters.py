"""Opt-in tool_contracts_v2: public feedback adapters, never simulator access.

These functions accept only the visible snapshot. They do not execute tools,
inspect apps/checkpoints, choose an action, or assert task completion. Hard
rejections use explicit, still-applicable public facts; unknown stays unknown.
"""
from __future__ import annotations
import json
import math

VERSION = "jev_tool_feedback_v2"
INSPECTIONS = {"inspect_emergence", "inspect_pests", "inspect_crop_health", "inspect_ridge", "fly_survey"}


def records(state):
    from .candidates import visible_records
    return visible_records(state)


def successful(row):
    from .candidates import _successful
    return _successful(row)


def number(value):
    return type(value) in (int, float) and math.isfinite(value)


def ref(row, field=None):
    return "tool_result:" + str(row.get("tool_call_id", row["tool_name"])) + (":" + field if field else "")


def intervals(ids):
    """Contiguous runs only: never fill unobserved gaps."""
    ids = sorted({x for x in ids if type(x) is int and x >= 0})
    result = []
    for x in ids:
        if result and result[-1][1] + 1 == x:
            result[-1][1] = x
        else:
            result.append([x, x])
    return result


def coverage(tool_name, state):
    covered, remaining, refs = set(), set(), []
    for row in records(state):
        # Do not confuse a pest survey or another robot with emergence coverage.
        if row["tool_name"] != tool_name or not successful(row):
            continue
        result = row.get("result")
        if not isinstance(result, dict):
            continue
        for field, target in [("covered_ridges", covered), ("uncovered_ridges", remaining)]:
            if isinstance(result.get(field), list):
                target.update(x for x in result[field] if type(x) is int and x >= 0)
                refs.append(ref(row, field))
    return {"covered": covered, "remaining": remaining-covered, "source_refs": list(dict.fromkeys(refs))}


def latest_device_row(tool_name, state):
    app = tool_name.split("__")[0]
    rows = [r for r in records(state) if r["tool_name"].split("__")[0] == app and successful(r)]
    return rows[-1] if rows else None


def charging_fact(tool_name, state):
    row = latest_device_row(tool_name, state)
    result = row.get("result") if row else None
    if not isinstance(result, dict):
        return None
    if result.get("charging") is not True and result.get("status") != "charging_started":
        return None
    deadline = result.get("charge_complete_at")
    if not number(deadline):
        eta, at = result.get("eta_minutes_to_full"), row.get("sim_time_after")
        deadline = at + eta*60 if number(eta) and eta >= 0 and number(at) else None
    now = state.get("current_time")
    # A historical charging flag with no usable time cannot block forever.
    active = number(deadline) and number(now) and now < deadline
    return {"status": "known_active" if active else "needs_status_refresh", "deadline": deadline,
            "source_refs": [ref(row)], "observed_at": row.get("sim_time_after")}


def wait_values(state):
    """Remaining deadlines from returned absolute times or timestamped ETAs.

    Later device status replaces older charge ETA; independent irrigation effects
    are retained. Relative ETAs without an observation timestamp are not invented.
    """
    now = state.get("current_time")
    if not number(now):
        return []
    values = []
    rows = records(state)
    latest = {}
    for row in rows:
        if successful(row):latest[row["tool_name"].split("__")[0]] = row
    for row in rows:
        result = row.get("result")
        if not successful(row) or not isinstance(result, dict):continue
        pairs = [("effect_ready_at", "eta_minutes_to_confirmation")]
        if latest.get(row["tool_name"].split("__")[0]) is row and (
                result.get("charging") is True or result.get("status") == "charging_started"):
            pairs.append(("charge_complete_at", "eta_minutes_to_full"))
        for absolute, relative in pairs:
            deadline, field = result.get(absolute), absolute
            if not number(deadline):
                eta, at = result.get(relative), row.get("sim_time_after")
                if not number(eta) or eta < 0 or not number(at):continue
                deadline, field = at + eta*60, relative
            if deadline > now:values.append((max(1, math.ceil(deadline-now)), ref(row, field)))
    return list(dict.fromkeys(values))


def precondition(tool_name, state):
    if tool_name.split("__")[-1] not in INSPECTIONS:return None
    fact = charging_fact(tool_name, state)
    if fact and fact["status"] == "known_active":
        return {"status": "needs_context", "candidates": {}, "missing_fields": [],
                "reason": "Selected device is charging according to visible timed feedback; wait or refresh status before inspection.",
                "precondition": "device_charging", "evidence": fact, "executed": False,
                "adapter_version": VERSION}
    return None


def annotate(tool_name, arguments, state):
    """Explain coverage; retain repeat observations as legal model choices."""
    if tool_name.split("__")[-1] not in INSPECTIONS:return None
    data = coverage(tool_name, state)
    start = arguments.get("start_ridge", arguments.get("ridge_id"))
    end = arguments.get("end_ridge", start)
    if type(start) is not int or type(end) is not int:return None
    selected = set(range(start, end+1))
    return {"prior_observed_coverage": intervals(data["covered"] & selected),
            "requested_ridges_not_previously_covered": intervals(selected-data["covered"]),
            "requested_new_count": len(selected-data["covered"]),
            "source_refs": data["source_refs"],
            "scope": "Coverage in retained visible records only; not a predicted single-pass result. Repeat observations remain allowed."}


def resource_check(tool_name, args, state):
    """Conservative fertilizer dose check from latest public tractor feedback.

    No hidden capacity/fuel constants. A later tractor operation without resource
    fields invalidates the earlier observation. Rounded amounts have a 0.05-unit
    uncertainty margin; rejection requires insufficiency even at its upper bound.
    """
    if tool_name != "TractorApp__apply_fertilizer":return None
    row = latest_device_row(tool_name, state)
    result = row.get("result") if row else None
    if not isinstance(result, dict):return {"status": "unknown", "source_refs": []}
    amount = result.get("fertilizer_spreader_kg")
    if not number(amount) or amount < 0:return {"status": "unknown", "source_refs": []}
    required = (args['end_ridge']-args['start_ridge']+1)*args['kg_per_ridge']
    return {"status": "known_insufficient" if required > amount + .050000001 else "not_proven_insufficient",
            "required_kg": required, "observed_kg": amount, "rounding_margin_kg": .05,
            "source_refs": [ref(row, "fertilizer_spreader_kg")],
            "scope": "Fertilizer only; fuel, trafficability, quotas and other conditions remain unchecked"}


def concise_result(value):
    """A bounded factual projection with explicit omissions, not LLM inference."""
    if not isinstance(value, dict):
        text = str(value)
        return {"returned_text": text[:500], "omitted_characters": max(0,len(text)-500)}
    facts = {}; omitted = []
    for key, val in value.items():
        if isinstance(val, (str,int,float,bool)) or val is None:
            if isinstance(val,str) and len(val)>500: facts[key]={'prefix':val[:500],'omitted_characters':len(val)-500}
            else:facts[key]=val
        elif isinstance(val,list) and ('ridges' in key or key=='ridge_ids') and all(type(x) is int for x in val):
            facts[key]={'count':len(set(val)), 'intervals':intervals(val)}
        elif key in {'ridges','observations'}:
            entries = val.values() if isinstance(val,dict) else val
            entries = list(entries) if isinstance(entries,(list,tuple)) or hasattr(entries,'__iter__') else []
            stands=[r['stand_fraction'] for r in entries if isinstance(r,dict) and number(r.get('stand_fraction'))]
            facts[key]={'records':len(entries),'stand_fraction_observed_count':len(stands)}
            if stands:facts[key].update(stand_fraction_min=min(stands),stand_fraction_max=max(stands))
            omitted.append(key+' (other nested fields)')
        else:omitted.append(key)
    if omitted:facts['omitted_fields']=omitted
    return facts


def factual_report(state):
    rows=[r for r in records(state) if r['tool_name']!='AgentUserInterface__send_message_to_user']
    # Latest instance of every observed tool plus errors: finite report, with
    # historical omission explicitly stated. No task-complete certification.
    latest={r['tool_name']:r for r in rows};selected=list(latest.values())
    facts=[{'tool':r['tool_name'],'call_id':r.get('tool_call_id'),'status':r.get('status','unknown'),
            'observed_at':r.get('sim_time_after'),'arguments':r.get('arguments',{}),
            'returned_facts':concise_result(r.get('result'))} for r in selected]
    body={'scope':'Visible tool evidence only; not independent task validation or agronomic inference',
          'visible_call_count':len(rows),'omitted_older_call_count':len(rows)-len(selected),
          'tool_facts':facts,
          'inspection_coverage':{name:intervals(coverage(name,state)['covered']) for name in latest if name.split('__')[-1] in INSPECTIONS},
          'limitations':'Unobserved facts remain unknown. Successful operations do not by themselves certify task completion.'}
    if len(json.dumps(body,ensure_ascii=False)) > 12000:
        body['detail_omission_reason'] = 'Report size bound; original results remain in trace'
        body['tool_facts'] = [{k:v for k,v in fact.items() if k not in {'returned_facts','arguments'}} for fact in facts]
    return '可见工具事实报告：'+json.dumps(body,ensure_ascii=False,sort_keys=True)
