"""Manual Linux integration check against exact 96cd8e9 code, entirely in /tmp.

Requires the base git object. No network, credentials, service or real DB access.
Run from the checkout: python3 tests/verify_readonly_overlay.py
"""
import hashlib
import importlib.util
import io
import os
from pathlib import Path
import subprocess
import sys
import tarfile
from tempfile import TemporaryDirectory

BASE='96cd8e9'
ROOT=Path(__file__).resolve().parents[1]
SMOKE=r'''
import io,json
from pathlib import Path
from src.gldrubf_bot.live_acceptance import ReadOnlyBroker
from src.gldrubf_bot.models import DataError,instant

calls=[]
def day(label,holiday=False):
    a,b=label+'T07:00:00Z',label+'T16:00:00Z'
    intervals=[dict(type='regular_trading_session',interval=dict(startTs=a,endTs=b))]
    if holiday:intervals.append(dict(type='holiday_trading_session_morning',interval=dict(startTs=a,endTs=b)))
    return dict(date=label+'T00:00:00Z',isTradingDay=True,startTime=a,endTime=b,intervals=intervals)
payload=dict(exchanges=[dict(exchange='FORTS_FUTURES_WEEKEND',days=[day('2026-11-19'),day('2026-11-21',True)])])
def transport(request,timeout):
    assert request.full_url.endswith('InstrumentsService/TradingSchedules')
    calls.append('TradingSchedules')
    stream=io.BytesIO(json.dumps(payload).encode());stream.status=200;stream.headers={'Content-Type':'application/json'}
    return stream
broker=ReadOnlyBroker('synthetic',transport=transport)
calendar=broker.current_calendar('forts_futures_weekend',instant('2026-11-19T10:00:00Z'))
assert any(d.contains(instant('2026-11-19T10:00:00Z')) for d in calendar.values())
assert broker.calendar_diagnostic['selection']['excluded_record_indexes']==[1]
try:broker.post({})
except DataError:pass
else:raise AssertionError('Write entry point not sealed')
assert calls==['TradingSchedules']
assert not list(Path('.').rglob('*.sqlite3'))
print('Updated read-only module imports and handles future holiday on frozen old core; HTTP fully mocked; writes denied.')
'''


def main():
    spec=importlib.util.spec_from_file_location('readonly_update',ROOT/'deploy/live/update_readonly.py')
    updater=importlib.util.module_from_spec(spec);spec.loader.exec_module(updater)
    archive=subprocess.check_output(['git','archive',BASE,'src'],cwd=ROOT)
    with TemporaryDirectory(prefix='readonly exact 96cd8e9 ') as tmp:
        root=Path(tmp);code=root/'opt/mx';code.mkdir(parents=True)
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:tar.extractall(code,filter='data')
        wrapper=root/'bin/gldrubf-live-check';wrapper.parent.mkdir()
        wrapper.write_bytes(subprocess.check_output(['git','show',BASE+':deploy/live/acceptance_check.sh'],cwd=ROOT))
        state=root/'state';state.mkdir();(state/'read_only.lock').touch()
        (root/'config.json').write_text('synthetic config unchanged');(root/'token').write_text('synthetic token unchanged')
        old={n:hashlib.sha256((code/'src/gldrubf_bot'/n).read_bytes()).hexdigest() for n in (*updater.FROZEN,'live_api.py')}
        backup=updater.update(ROOT,code,wrapper,state)
        assert sorted(p.name for p in backup.iterdir())==['live_acceptance.py','market_diagnostics.py']
        updater.update(ROOT,code,wrapper,state)
        for n,h in old.items():assert hashlib.sha256((code/'src/gldrubf_bot'/n).read_bytes()).hexdigest()==h,n
        assert (root/'config.json').read_text()=='synthetic config unchanged'
        assert (root/'token').read_text()=='synthetic token unchanged'
        assert sorted(p.name for p in state.iterdir())==['read_only.lock']
        env=dict(os.environ,T_INVEST_TOKEN='synthetic-overlay-token',PYTHONDONTWRITEBYTECODE='1')
        for key in ('PYTHONPATH','PYTHONHOME','SSLKEYLOGFILE'):env.pop(key,None)
        subprocess.run([sys.executable,'-c',SMOKE],cwd=code,env=env,check=True)
    print('Exact 96cd8e9 overlay/repeat PASSED; no trading core, config, token, SQLite or service changed.')


if __name__=='__main__':main()
