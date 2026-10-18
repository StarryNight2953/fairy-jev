"""C5 artifact-only validation, source preservation and export; no live imports."""
from datetime import datetime, timezone, timedelta
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile

P=Path(__file__).resolve().parent
ROOT=P.parents[2]
DOCS=ROOT/'docs/experiments/hn58_tool_description_v2_20261019'
def now():return datetime.now(timezone(timedelta(hours=8))).isoformat()
def read(p):return json.loads(Path(p).read_text())
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def write(p,d):Path(p).write_text(json.dumps(d,ensure_ascii=False,indent=2)+'\n')
def git(*args,cwd=ROOT):return subprocess.check_output(['git',*args],cwd=cwd,text=True).strip()

def verify():
    original=read(P/'c0_snapshot.json'); identity=read(P/'experiment_manifest.json')
    changes=[p for p,h in original['protected_files'].items() if not Path(p).is_file() or sha(p)!=h]
    assert not changes,changes
    trees={}
    for name in ('jev-mvp','hn58-reproduce'):
        before=original['worktrees'][name]; w=ROOT.parent/name
        actual={'head':git('rev-parse','HEAD',cwd=w),'status':git('status','--short',cwd=w)}
        assert actual['head']==before['head'] and actual['status']==before['status'].strip()
        trees[name]=actual
    assert git('rev-parse','refs/heads/feat/llm-jev-hybrid-hn58')==original['worktrees']['llm-jev-hybrid']['head']
    assert git('rev-parse','refs/tags/experiment/hn58-tool-description-v2-20261019')==identity['experiment_commit']
    assert not git('diff',identity['experiment_commit'],'--','fairy','configs','tests','pyproject.toml','uv.lock')
    ledger=read(original['ledger_path']); after=read(P/'budget_after.json')
    assert ledger==after['ledger'] and ledger['episodes'][:6]==original['budget_before']['episodes']
    assert ledger['http_attempts']==33 and len(ledger['episodes'])==7
    index=read(P/'evidence_index.json');file_checks=0
    for run in index['runs']:
        for f in run['files']:
            assert sha(f['path'])==f['sha256'],f['path'];file_checks+=1
    for old in index['old_controls']:
        for f in old['archive_copies']:
            assert sha(f['source'])==sha(f['archive_copy'])==f['sha256'];file_checks+=1
    refs=0
    def check(value):
        nonlocal refs
        if isinstance(value,dict):
            if all(k in value for k in ('path','sha256','event_id','jsonl_line')):
                path=Path(value['path']);assert sha(path)==value['sha256']
                e=json.loads(path.read_text().splitlines()[value['jsonl_line']-1]);assert e['event_id']==value['event_id'];refs+=1
            for v in value.values():check(v)
        elif isinstance(value,list):
            for v in value:check(v)
    check(read(P/'comparison.json'))
    for f in (P/'analysis').glob('*.json'):
        d=read(f);check(d)
        if 'events' in d:
            source=[json.loads(x) for x in Path(d['source_trace']).read_text().splitlines()]
            for row in d['events']:assert row['event']==source[row['reference']['jsonl_line']-1]
    for file in ('report.md','preflight_report.md'):
        for match in re.finditer(r'\]\((/[^)]+)\)',(P/file).read_text()):
            assert Path(match.group(1)).exists(),match.group(1)
    # Check known credential byte strings without printing values or hashes.
    secrets=[v.encode() for k,v in os.environ.items() if k.endswith(('API_KEY','API_TOKEN','ACCESS_TOKEN','SECRET')) and v and len(v)>=12 and 'placeholder' not in v]
    matches=[]
    for file in P.rglob('*'):
        if file.is_file() and any(v in file.read_bytes() for v in secrets):matches.append(str(file.relative_to(P)))
    assert not matches,'Credential content detected in an artifact; filenames only: '+repr(matches)
    write(P/'preservation.json',{'checked_at':now(),'passed':True,'protected_original_files':len(original['protected_files']),'all_original_hashes_unchanged':True,
        'changes':[],'other_worktrees_head_status_unchanged':trees,'original_hybrid_branch_ref_unchanged':True,
        'experiment_runtime_source_unchanged_since_freeze':True,'frozen_tag_unchanged':True,
        'historical_ledger_entries_unchanged':True,'ledger_before':{'episodes':6,'http':30},'ledger_after':{'episodes':7,'http':33},
        'only_new_experiment_consumption':{'episodes':1,'http':3},'existing_map_sha256':sha(ROOT.parent/'workspace-paths.json'),
        'private_key_values_saved':False,'secret_scan_matches':matches,'secret_scan_scope':'Known process credential values; values/hashes never recorded, no authentication headers captured',
        'zero_extra_api_probes_or_replacement_samples':True})
    write(P/'delivery_validation.json',{'checked_at':now(),'passed':True,'protected_original_hash_count':len(original['protected_files']),
        'source_and_copy_hash_checks':file_checks,'trace_reference_checks':refs,'window_events_equal_originals':True,
        'report_file_links_exist':True,'live_budget_and_counts_consistent':True,'runtime_source_equal_frozen':True,
        'secret_scan_passed':True,'model_calls_during_validation':0,'environment_calls_during_validation':0,
        'scope':'Artifact checks only; no new tests, replay, scenario initialization, SDK/client or network calls.'})
    print(json.dumps({'validation':'passed','protected_original_files':len(original['protected_files']),'evidence_files':file_checks,'trace_refs':refs,'budget':'7/12 episodes;33/1440 HTTP'}))

def docs():
    assert not DOCS.exists()
    DOCS.mkdir(parents=True)
    for n in ('report.md','preflight_report.md','experiment_manifest.json','comparison.json','evidence_index.json','budget_after.json','preservation.json','delivery_validation.json','commands.json','user_docstring.diff'):
        shutil.copyfile(P/n,DOCS/n)
    (DOCS/'orchestration').mkdir()
    for n in ('offline_checks.py','run_regression.py','run_registered.py','analyze_results.py','write_delivery.py','finalize_delivery.py'):
        shutil.copyfile(P/n,DOCS/'orchestration'/n)
    (DOCS/'README.md').write_text(f'''# HN58 Tool Description v2 版本归档

运行提交：bbe762c98a97e5f22cceb5f7b24fc3a67656fdb6；冻结标签 refs/tags/experiment/hn58-tool-description-v2-20261019。

请先读 [report.md](report.md)。一组原生Qwen真实任务失败，两组Jev/Hybrid访问门禁阻塞；不是完成的三组真实对照。

完整原始证据：`{P}`。同级 `.tar.gz` 压缩包包含本轮全部live/scripted/模型请求响应、六份旧对照原字节副本和SHA256清单。大证据保存在runs，未全部提交Git。

本目录是C5时的说明及结果快照，原始结果以runs中的manifest/trace为准。orchestration/是已执行脚本的版本化副本，保留原脚本SHA对应关系；不要在这里执行live脚本或复用已消耗slot。没有再运行授权，也不会自动补测。旧证据内绝对路径保留，workspace-paths.json用于迁移解析。
''')
    print('Version archive prepared at '+str(DOCS))

def package():
    assert not git('status','--porcelain')
    identity=read(P/'experiment_manifest.json')
    bundle=P.with_suffix('.tar.gz')
    assert not bundle.exists()
    version={'archived_at':now(),'experiment_commit':identity['experiment_commit'],'experiment_tag':identity['experiment_tag'],
        'delivery_commit':git('rev-parse','HEAD'),'branch':git('branch','--show-current'),'git_status':'clean','versioned_docs':str(DOCS),
        'evidence_directory':str(P),'archive_path':str(bundle),'archive_sha256_file':str(bundle)+'.sha256',
        'experiment_source_diff_after_freeze':git('diff',identity['experiment_commit'],'HEAD','--','fairy','configs','tests','pyproject.toml','uv.lock'),
        'tag_points_to_original_experiment':git('rev-parse','refs/tags/'+identity['experiment_tag'])==identity['experiment_commit'],
        'note':'Delivery commit contains docs and orchestration copies only. Do not rewrite original execution commit to this documentation commit.'}
    assert not version['experiment_source_diff_after_freeze'] and version['tag_points_to_original_experiment']
    write(P/'version_archive.json',version)
    files=[f for f in sorted(P.rglob('*')) if f.is_file() and f!=P/'files.sha256']
    (P/'files.sha256').write_text(''.join(f'{sha(f)}  {f.relative_to(P)}\n' for f in files))
    with tarfile.open(bundle,'w:gz') as tf:tf.add(P,arcname=P.name)
    hash_value=sha(bundle)
    Path(str(bundle)+'.sha256').write_text(f'{hash_value}  {bundle.name}\n')
    checked=0
    with tarfile.open(bundle,'r:gz') as tf:
        for line in (P/'files.sha256').read_text().splitlines():
            expected,name=line.split('  ',1)
            assert hashlib.sha256(tf.extractfile(P.name+'/'+name).read()).hexdigest()==expected
            checked+=1
    print(json.dumps({'archive':str(bundle),'sha256':hash_value,'payload_hashes_verified':checked,'experiment_commit':identity['experiment_commit'],'delivery_commit':version['delivery_commit'],'archive_bytes':bundle.stat().st_size},ensure_ascii=False))

if __name__=='__main__':{'verify':verify,'docs':docs,'package':package}[sys.argv[1]]()
