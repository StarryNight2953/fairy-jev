"""Run the existing applicable offline regression with network connections denied."""
from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime, timezone, timedelta
import json
import os
from pathlib import Path
import socket
import time
import pytest

out=Path(__file__).resolve().parent
def reject(*args,**kwargs):
    raise AssertionError('Offline regression: network connection denied')
socket.socket.connect=reject
socket.socket.connect_ex=reject
socket.create_connection=reject
os.environ['DEEPSEEK_API_KEY']='offline-regression-placeholder'
os.environ['DASHSCOPE_API_KEY']='offline-regression-placeholder'
args=['-q','tests/jev','tests/test_replay_contract.py','tests/test_fairy_migration_smoke.py']
started=time.monotonic()
with (out/'regression.log').open('x') as stream,redirect_stdout(stream),redirect_stderr(stream):
    result=pytest.main(args)
record={'timestamp':datetime.now(timezone(timedelta(hours=8))).isoformat(),'command':'pytest '+' '.join(args),'exit_code':int(result),
    'wall_seconds':time.monotonic()-started,'log_path':str(out/'regression.log'),'network_guard':'socket.connect/connect_ex/create_connection denied in pytest process',
    'model_http_attempts':0,'note':'Test fake HTTP transports are offline fixtures; no service calls or task ledger reservations.'}
(out/'regression_result.json').write_text(json.dumps(record,ensure_ascii=False,indent=2)+'\n')
print(json.dumps(record,ensure_ascii=False))
raise SystemExit(int(result))
