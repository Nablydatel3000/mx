# Воспроизведение сравнения 531f527 и 0f52907

Запускать из корня репозитория. Нужны локальные git-объекты обоих коммитов.
Runtime-файлы должны соответствовать 0f52907; скрипт проверяет это побайтно.
Это только документированный аудиторский расчёт, не новая функция робота.
История берётся из сохранённого реального архива, цены не генерируются.
SQLite создаётся исключительно во временном каталоге; Engine и брокер не запускаются.
Техническая подача исторических префиксов не моделирует реальные времена публикации/исполнения.

```bash
PYTHONDONTWRITEBYTECODE=1 python3 - <<'PY'
"""Audit calculation only: no Engine, broker, API, orders or real database."""
import csv
from datetime import date
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import types

root=Path.cwd();sys.path.insert(0,str(root))
from audits.gldrubf_real_history.audit import unpack,calculate
from src.gldrubf_bot.history import load_snapshot
from src.gldrubf_bot.models import instant
from src.gldrubf_bot.strategy import DailyClose,crossover
from src.gldrubf_bot.live_store import Store
from src.gldrubf_bot.live import fingerprint
from src.gldrubf_bot.live_acceptance import PROPOSED

reference='531f527';runtime='0f52907'
for path in subprocess.check_output(['git','ls-tree','-r','--name-only',runtime,'src'],text=True).splitlines():
    assert (root/path).read_bytes()==subprocess.check_output(['git','show',runtime+':'+path]),path
archive=root/'audits/gldrubf_real_history/source/GLDRUBF_history.tar.gz'
archive_sha=hashlib.sha256(archive.read_bytes()).hexdigest()
assert archive_sha=='498e4aacb2da1d041a93efa34de807945f171fd44791541a14735a279e05ee7d'
sources={}
for path in ('src/gldrubf_bot/strategy.py','audits/gldrubf_real_history/audit.py','audits/sma20_50_scenario/run.py'):
    old=subprocess.check_output(['git','show',reference+':'+path]);new=(root/path).read_bytes()
    assert old==new,path
    sources[path]=hashlib.sha256(new).hexdigest()
module=types.ModuleType('reference_strategy_531f527');sys.modules[module.__name__]=module
old=subprocess.check_output(['git','show',reference+':src/gldrubf_bot/strategy.py'])
exec(compile(old,'531f527/strategy.py','exec'),module.__dict__)
with TemporaryDirectory(prefix='real signal identity ') as temporary:
    temp=Path(temporary);snapshot,_=unpack(archive,temp/'source')
    manifest,all_rows=load_snapshot(snapshot);complete=[r for r in all_rows if r['is_complete']]
    daily,signals,exact=calculate(all_rows)  # File bytes verified against reference above.
    reference_history=[];current_history=[];prefix_checks=0
    for row in complete:
        label=date.fromisoformat(row['provider_day']);price=float(row['close'])
        reference_history.append(module.DailyClose(label,price));current_history.append(DailyClose(label,price))
        if len(current_history)>=51:
            assert module.crossover(reference_history).target==crossover(current_history).target
            prefix_checks+=1
    c=dict(PROPOSED,account_id='12345678');known=instant(manifest['captured_at'])
    store=Store(temp/'prefix.sqlite3',fingerprint(c),instant(manifest['from']),known)
    store.ingest(complete[:50],known)
    for n in range(51,len(complete)+1):store.ingest(complete[:n],known)
    with store.db() as db:actual=[dict(r) for r in db.execute('SELECT day,target FROM live_signals ORDER BY day')]
    expected=[dict(day=r['provider_day'],target=2*int(r['exact_target'])) for r in signals]
    assert actual==expected
    store.ingest(all_rows,known)  # The real unfinished record must have no effect.
    with store.db() as db:
        assert db.execute('SELECT COUNT(*) FROM live_bars').fetchone()[0]==len(complete)
        assert db.execute('SELECT COUNT(*) FROM live_signals').fetchone()[0]==len(signals)
    bootstrap=Store(temp/'full_bootstrap.sqlite3',fingerprint(c),instant(manifest['from']),known)
    bootstrap.ingest(all_rows,known)
    with bootstrap.db() as db:bootstrap_signals=db.execute('SELECT COUNT(*) FROM live_signals').fetchone()[0]
    assert bootstrap_signals==0 and bootstrap.position()==0
    result=dict(reference_commit=subprocess.check_output(['git','rev-parse',reference],text=True).strip(),
        runtime_commit=subprocess.check_output(['git','rev-parse',runtime],text=True).strip(),
        source_archive_sha256=archive_sha,source_hashes=sources,
        all_runtime_sources_match_pinned_commit=True,
        complete_count=len(complete),first=complete[0]['provider_day'],last=complete[-1]['provider_day'],
        incomplete_excluded=[r['provider_day'] for r in all_rows if not r['is_complete']],
        eligible_prefixes=prefix_checks,signal_count=len(signals),
        long_count=sum(r['exact_target']==1 for r in signals),short_count=sum(r['exact_target']==-1 for r in signals),
        reference_current_direction_mismatches=0,reference_persistent_live_target_mismatches=0,
        persistent_live_replay_bootstrap_candles=50,full_snapshot_bootstrap_signals=bootstrap_signals,
        full_snapshot_bootstrap_ledger_position=bootstrap.position(),
        exact_arithmetic_comparison=exact,
        observation_clock='All test prefixes ingested at archive captured_at; technical identity, NOT historical publication or execution replay.',
        real_api_used=False,live_engine_started=False,orders_created=0,
        jino_old_live_database_exists='UNKNOWN: no VPS access',
        execution_equivalence=False,
        execution_difference='Reference: one contract at next DAY open, no costs. LIVE: up to two contracts, confirmed flat before reversal, fresh LIMIT IOC gates; no identical fill/PnL claim.')
    comparison=[dict(provider_day=r['provider_day'],signal=r['signal'],sma20=r['sma20'],sma50=r['sma50'],
        reference_target=r['exact_target'],live_target=a['target'],direction_matches=True) for r,a in zip(signals,actual)]
out=root/'audits/live_reference_531f527';out.mkdir(parents=True,exist_ok=True)
with (out/'signal_identity.csv').open('w',newline='') as stream:
    writer=csv.DictWriter(stream,fieldnames=list(comparison[0]),lineterminator='\n');writer.writeheader();writer.writerows(comparison)
(out/'identity.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
print(json.dumps(result,ensure_ascii=False,indent=2))
PY
```

Повторить только три заданных критических условия существующими тестами:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 - <<'PY'
import json, subprocess
from pathlib import Path
names=json.loads(Path('audits/live_reference_531f527/CHECKS.json').read_text())['tests']
subprocess.run(['python3','-W','error::ResourceWarning','-m','unittest',*names,'-v'],check=True)
PY
```

Все брокерские ответы и разрешения в этих тестах имитируются во временных
каталогах. Боевой токен не нужен. Команд подключения к Jino, запуска LIVE,
изменения существующей базы и торговых поручений в воспроизведении нет.
