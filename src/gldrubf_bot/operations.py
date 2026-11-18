"""Private runtime heartbeat, durable alert outbox, bounded local diagnostics."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
from datetime import datetime
import urllib.request
import urllib.error
from urllib.parse import urlsplit
from uuid import uuid4

from .api import NoRedirect
from .models import DataError, UTC, instant, iso


def atomic_json(path, data):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_name(path.name+'.'+uuid4().hex+'.tmp')
    try:
        with temporary.open('x',encoding='utf-8',newline='\n') as stream:
            os.chmod(temporary,0o600)
            json.dump(data,stream,ensure_ascii=False,sort_keys=True)
            stream.write('\n');stream.flush();os.fsync(stream.fileno())
        os.replace(temporary,path)
    finally:
        temporary.unlink(missing_ok=True)


def process_identity(pid):
    try:
        os.kill(pid,0)
        return Path(f'/proc/{pid}/stat').read_text().rpartition(')')[2].split()[19]
    except (OSError,IndexError,ValueError):
        return None


def enqueue(root, key, event):
    root=Path(root);root.mkdir(parents=True,exist_ok=True)
    name=hashlib.sha256(key.encode()).hexdigest()+'.json'
    path=root/name
    if not path.exists():
        if len(list(root.glob('*.json')))>=1000:
            raise DataError('Alert outbox full; operator action required')
        atomic_json(path,event)


class Observer:
    def __init__(self,state,output,*,retention_days,min_free_mb,clock=lambda:datetime.now(UTC)):
        self.state,self.output=Path(state),Path(output)
        self.clock=clock;self.retention_days=retention_days;self.min_free_mb=min_free_mb
        self.state.mkdir(parents=True,exist_ok=True)
        now=iso(clock())
        self.data=dict(schema=1,run_id=uuid4().hex,pid=os.getpid(),process_identity=process_identity(os.getpid()),
                       started_at=now,updated_at=now,last_healthy_at=None,last_poll_at=None,status='STARTING',event=None)

    def __call__(self,payload):
        now=instant(self.clock());status=payload['status']
        self.data.update(updated_at=iso(now),status=status)
        if status=='FETCHING':
            if (self.state/'restore_review_required.json').exists():
                raise DataError('Restored ledger requires reconciliation before execution')
            prune(self.output,now,self.retention_days)
            if shutil.disk_usage(self.state).free<self.min_free_mb*1024*1024:
                raise DataError('Disk reserve below configured minimum; execution blocked')
        if payload.get('event') is not None:self.data['event']=payload['event']
        if status=='READY':atomic_json(self.state/'ledger_initialized.json',dict(initialized=True))
        if isinstance(payload.get('complete_history_age'),dict):
            self.data['complete_history_age']={k:v for k,v in payload['complete_history_age'].items()
                if k in ('latest_complete','age_seconds','maximum_hours','fresh')}
        if status=='RUNNING':
            self.data['last_poll_at']=iso(now)
            summary=payload.get('summary',{})
            for name in ('position','pending','pending_day','fills','mark_time','bootstrap_origin','bootstrap_through','migration_review_required'):
                self.data[name]=summary.get(name)
            if self.data['event'] in ('NO_FILL','PAPER_FILL','WAIT_MARKET'):
                self.data['last_healthy_at']=iso(now)
            if self.data['event']=='PAPER_FILL':
                enqueue(self.state/'outbox',self.data['run_id']+':fill:'+str(summary['fills']),
                        dict(event='PAPER_FILL',time=iso(now),position=summary['position'],fills=summary['fills']))
        atomic_json(self.state/'health.json',self.data)
        if status=='RUNNING':prune(self.output,now,self.retention_days)


def prune(output,now,days):
    """Explicit operator retention, never prune the last two source snapshots."""
    cutoff=instant(now).timestamp()-days*86400
    histories=Path(output)/'history'
    if histories.exists():
        paths=sorted(p for p in histories.iterdir() if p.is_dir() and not p.is_symlink() and
                     re.fullmatch(r'\d{8}T\d{6}_\d{6}Z',p.name))
        valid=[]
        for path in paths:
            try:
                if json.loads((path/'manifest.json').read_text()).get('status')=='valid_with_limits':valid.append(path)
            except (OSError,ValueError):pass
        protected=set(paths[-2:]+valid[-2:])
        for path in paths:
            if path in protected:continue
            if path.stat().st_mtime<cutoff:shutil.rmtree(path)
    checks=Path(output)/'forward_checks'
    if checks.exists():
        for path in checks.glob('*.json'):
            if not path.is_symlink() and re.fullmatch(r'[0-9a-f]{64}\.json',path.name) and path.stat().st_mtime<cutoff:
                path.unlink()


def assess(data,now,*,max_age=600,grace=300,identity=process_identity):
    now=instant(now)
    try:
        age=(now-instant(data['updated_at'])).total_seconds()
        if age<0:return dict(healthy=False,reason='clock_or_future_heartbeat')
        if age>max_age:return dict(healthy=False,reason='heartbeat_stale')
        if not data.get('process_identity') or identity(data['pid'])!=data['process_identity']:
            return dict(healthy=False,reason='process_missing_or_replaced')
        if data['status'] in ('STOP','STOPPED'):
            return dict(healthy=False,reason=data['status'])
        started=instant(data['started_at'])
        history=data.get('complete_history_age')
        if isinstance(history,dict) and history.get('fresh') is False:
            return dict(healthy=False,reason='completed_history_stale')
        if not data.get('last_healthy_at') and (now-started).total_seconds()<=grace:
            return dict(healthy=False,reason='waiting_first_success',pending=True)
        healthy_at=instant(data['last_healthy_at']) if data.get('last_healthy_at') else started
        if healthy_at>now:return dict(healthy=False,reason='clock_or_future_healthy_time')
        if (now-healthy_at).total_seconds()>grace:
            return dict(healthy=False,reason='no_healthy_poll')
        return dict(healthy=True,reason='within_healthy_poll_budget')
    except (KeyError,TypeError,ValueError,OSError):
        return dict(healthy=False,reason='invalid_heartbeat')


def monitor(state,now,*,max_age=600,grace=300):
    root=Path(state)
    try:data=json.loads((root/'health.json').read_text())
    except (OSError,ValueError):data={}
    result=assess(data,now,max_age=max_age,grace=grace)
    key=('healthy' if result['healthy'] else 'unhealthy')+':'+result['reason']
    previous=root/'monitor.json'
    try:old=json.loads(previous.read_text());last=old.get('key');sequence=int(old.get('sequence',0))
    except (OSError,ValueError,TypeError):last=None;sequence=0
    if key!=last:
        sequence+=1
        enqueue(root/'outbox',str(sequence)+':'+key,dict(event='HEALTHY' if result['healthy'] else 'STARTING' if result.get('pending') else 'ALERT',
                reason=result['reason'],time=iso(now)))
    atomic_json(previous,dict(key=key,sequence=sequence,checked_at=iso(now),**result))
    return result


def credential(name):
    directory=os.environ.get('CREDENTIALS_DIRECTORY')
    if not directory:raise DataError('systemd credentials required')
    path=Path(directory)/name
    if path.stat().st_size>16384:raise DataError('Credential too large')
    return path.read_text().strip()


def deliver(state,channel,*,transport=None):
    """At-least-once delivery. No URL, credential or response body in diagnostics."""
    if channel=='telegram':
        token=credential('alert-token');chat=credential('alert-chat')
        if not re.fullmatch(r'\d+:[A-Za-z0-9_-]+',token) or not re.fullmatch(r'-?\d+',chat):
            raise DataError('Invalid Telegram notification credentials')
        url='https://api.telegram.org/bot'+token+'/sendMessage'
    elif channel=='webhook':
        url=credential('alert-url');parts=urlsplit(url)
        if parts.scheme!='https' or not parts.hostname or parts.username or parts.password or parts.fragment:
            raise DataError('HTTPS notification URL required')
    else:raise DataError('Notification channel must be explicit')
    send=transport or urllib.request.build_opener(NoRedirect()).open
    root=Path(state);outbox=root/'outbox';delivered=0
    for path in sorted(outbox.glob('*.json'))[:10]:
        if path.is_symlink() or not re.fullmatch(r'[0-9a-f]{64}\.json',path.name):continue
        event=json.loads(path.read_text())
        body=(dict(chat_id=chat,text='GLDRUBF PAPER '+json.dumps(event,ensure_ascii=False)) if channel=='telegram' else
              dict(id=path.stem,source='GLDRUBF PAPER',**event))
        request=urllib.request.Request(url,data=json.dumps(body).encode(),headers={'Content-Type':'application/json'},method='POST')
        try:
            with send(request,timeout=10) as response:
                if not 200<=response.status<300:raise OSError('Notification HTTP failure')
                if channel=='telegram' and json.loads(response.read(65536)).get('ok') is not True:
                    raise OSError('Telegram did not acknowledge notification')
            path.unlink();delivered+=1
            atomic_json(root/'delivery.json',dict(last_success_at=iso(datetime.now(UTC)),category='delivered'))
        except Exception as error:
            if isinstance(error,urllib.error.HTTPError):error.close()
            # Never serialize exception.url/reason/body: endpoints can contain secrets.
            atomic_json(root/'delivery.json',dict(last_attempt_at=iso(datetime.now(UTC)),category='delivery_failed'))
            break
    return delivered
