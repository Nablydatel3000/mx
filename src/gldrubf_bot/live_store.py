"""Private transactional LIVE intent/execution ledger; never a PAPER database."""
from contextlib import contextmanager
from datetime import date
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from uuid import UUID,uuid4

from .api import quotation
from .history import candle_identity,candle_hash,encoded
from .models import DataError,instant,iso,number
from .strategy import DailyClose,crossover
from .live_api import UID,integer,rub
from .operations import atomic_json

TERMINAL={'FILL','REJECTED','CANCELLED','NOT_SENT'}
STATUS={'EXECUTION_REPORT_STATUS_'+k:k for k in ('FILL','REJECTED','CANCELLED','NEW','PARTIALLYFILL')}


class Store:
    def __init__(self,path,config_hash,source_from,now,*,allow_empty_new=False):
        self.path=Path(path).resolve();self.path.parent.mkdir(parents=True,exist_ok=True)
        if self.path.exists() and self.path.stat().st_size==0 and not allow_empty_new:
            raise DataError('Existing empty LIVE state; explicit new initialization required')
        if self.path.exists() and self.path.stat().st_size:
            # No old-state adoption, schema edits or rehashing before version verification.
            con=sqlite3.connect(self.path.as_uri()+'?mode=ro',uri=True)
            try:
                row=con.execute('SELECT config_hash FROM live_meta WHERE id=1').fetchone()
                if row is None or row[0]!=config_hash:raise DataError('LIVE state version mismatch; offline review required, no automatic migration')
            except sqlite3.DatabaseError:
                raise DataError('Unrecognized existing LIVE state; no automatic initialization') from None
            finally:con.close()
        with self.db() as db:
            tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            required={'live_meta','live_bars','live_signals','live_orders','live_trades','live_events','live_snapshots'}
            if tables and tables!=required:raise DataError('Not a complete LIVE ledger; PAPER adoption forbidden')
            db.execute('CREATE TABLE IF NOT EXISTS live_meta(id INTEGER PRIMARY KEY CHECK(id=1),config_hash TEXT,ledger_id TEXT,started TEXT,source_from TEXT,bootstrap TEXT,halt TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS live_bars(day TEXT PRIMARY KEY,payload TEXT,hash TEXT,known TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS live_signals(day TEXT PRIMARY KEY,target INTEGER,known TEXT,done INTEGER DEFAULT 0)')
            db.execute('''CREATE TABLE IF NOT EXISTS live_orders(request_id TEXT PRIMARY KEY,broker_id TEXT UNIQUE,
                signal_day TEXT,phase TEXT,side INTEGER,qty INTEGER,before_pos INTEGER,payload TEXT,
                created TEXT,status TEXT,executed INTEGER DEFAULT 0,commission TEXT,service_commission TEXT,
                cancel_requested INTEGER DEFAULT 0, context TEXT, UNIQUE(signal_day,phase))''')
            db.execute('CREATE TABLE IF NOT EXISTS live_trades(trade_id TEXT,request_id TEXT,time TEXT,qty INTEGER,price TEXT,currency TEXT,PRIMARY KEY(request_id,trade_id))')
            db.execute('CREATE TABLE IF NOT EXISTS live_events(id INTEGER PRIMARY KEY,time TEXT,category TEXT,details TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS live_snapshots(id INTEGER PRIMARY KEY,time TEXT,kind TEXT,payload TEXT)')
            meta=db.execute('SELECT * FROM live_meta WHERE id=1').fetchone()
            if meta is None:
                if tables:raise DataError('Missing LIVE metadata; no reset')
                db.execute('INSERT INTO live_meta VALUES(1,?,?,?,?,NULL,NULL)',(config_hash,str(uuid4()),iso(now),iso(source_from)))
            elif meta['config_hash']!=config_hash:raise DataError('LIVE configuration changed; no silent revaluation/adoption')
            self.validate(db)
        if os.name=='posix':self.path.chmod(0o600)
        marker=self.path.with_suffix('.highwater.json')
        orders=self.orders();ids={r['request_id'] for r in orders}
        if marker.exists():
            saved=json.loads(marker.read_text())
            if saved['ledger_id']!=self.meta()['ledger_id'] or not set(saved['request_ids'])<=ids or any(r['status']!='SENDING' and r['request_id'] not in saved['request_ids'] for r in orders):
                raise DataError('LIVE ledger rollback/highwater mismatch')
        elif orders:raise DataError('LIVE intent highwater missing')
        else:atomic_json(marker,dict(ledger_id=self.meta()['ledger_id'],request_ids=[]))

    @contextmanager
    def db(self):
        con=sqlite3.connect(self.path,timeout=15);con.row_factory=sqlite3.Row
        try:
            con.execute('PRAGMA synchronous=FULL');con.execute('BEGIN IMMEDIATE')
            yield con;con.commit()
        except BaseException:con.rollback();raise
        finally:con.close()

    def meta(self):
        with self.db() as db:return dict(db.execute('SELECT * FROM live_meta WHERE id=1').fetchone())

    def validate(self,db):
        if db.execute('PRAGMA quick_check').fetchone()[0]!='ok':raise DataError('LIVE SQLite corruption')
        meta=db.execute('SELECT * FROM live_meta WHERE id=1').fetchone();UUID(meta['ledger_id']);instant(meta['started']);instant(meta['source_from'])
        bars=list(db.execute('SELECT * FROM live_bars ORDER BY day'));hist=[];known=None
        for b in bars:
            row=json.loads(b['payload'])
            if candle_hash(row)!=b['hash'] or not row['is_complete'] or row['uid']!=UID or row['provider_day']!=b['day']:
                raise DataError('LIVE candle identity corrupted')
            t=instant(b['known'])
            if t<instant(row['time']) or (known and t<known):raise DataError('LIVE candle chronology')
            known=t;hist.append(DailyClose(date.fromisoformat(b['day']),float(row['close'])))
            signal=db.execute('SELECT * FROM live_signals WHERE day=?',(b['day'],)).fetchone()
            expected=crossover(hist).target*2 if len(hist)>=51 else 0
            eligible=meta['bootstrap'] and b['day']>meta['bootstrap']
            if bool(signal)!=bool(expected and eligible) or (signal and (signal['target']!=expected or signal['known']!=b['known'])):
                raise DataError('LIVE signal provenance corrupted')
        if bars and meta['bootstrap'] not in {b['day'] for b in bars}:raise DataError('Missing LIVE bootstrap')
        if meta['bootstrap'] and not bars:raise DataError('Bootstrap without history')
        if any(r['day'] not in {b['day'] for b in bars} or r['target'] not in (-2,2) or r['done'] not in (0,1) for r in db.execute('SELECT * FROM live_signals')):raise DataError('Orphan/invalid LIVE signal')
        running_position=0
        order_rows=list(db.execute('SELECT * FROM live_orders ORDER BY rowid'))
        for index,r in enumerate(order_rows):
            UUID(r['request_id']);payload=json.loads(r['payload'])
            if r['status'] not in TERMINAL|{'SENDING','UNKNOWN','NEW','PARTIALLYFILL'}:raise DataError('Invalid LIVE order state')
            if r['side'] not in (-1,1) or r['qty'] not in (1,2) or abs(r['before_pos'])>2 or abs(r['before_pos']+r['side']*r['qty'])>2:
                raise DataError('LIVE exposure corrupted')
            if r['before_pos']!=running_position or (r['status'] not in TERMINAL and index!=len(order_rows)-1):raise DataError('LIVE order sequence corrupted')
            running_position+=r['side']*r['executed']
            if abs(running_position)>2:raise DataError('Historical LIVE position limit violated')
            if (r['phase']=='OPEN' and (r['before_pos']!=0 or r['qty']!=2)) or (r['phase']=='CLOSE' and r['before_pos']+r['side']*r['qty']!=0):
                raise DataError('LIVE leg sequence corrupted')
            if r['phase'] not in ('OPEN','CLOSE') or payload.get('orderId')!=r['request_id'] or payload.get('instrumentId')!=UID or integer(payload.get('quantity'))!=r['qty'] or payload.get('direction')!=('ORDER_DIRECTION_BUY' if r['side']==1 else 'ORDER_DIRECTION_SELL') or payload.get('orderType')!='ORDER_TYPE_LIMIT' or payload.get('confirmMarginTrade') is not False:
                raise DataError('LIVE payload corrupted')
            context=json.loads(r['context'])
            if not instant(context['window_from'])<=instant(r['created'])<instant(context['window_to']):raise DataError('LIVE dispatch window corrupted')
            if context.get('status_gate')!='UID_NORMAL_API_LIMIT_CONFIRMED' or not instant(context['cycle_from'])<=instant(context['calendar_from'])<=instant(context['status_from'])<=instant(context['status_to'])<=instant(r['created']):
                raise DataError('LIVE status evidence corrupted')
            sig=db.execute('SELECT * FROM live_signals WHERE day=?',(r['signal_day'],)).fetchone()
            if not sig or sig['target']*r['side']<=0 or instant(r['created'])<=instant(sig['known']):raise DataError('LIVE order signal causality')
            trades=list(db.execute('SELECT * FROM live_trades WHERE request_id=?',(r['request_id'],)))
            if r['status']=='NOT_SENT' and (trades or r['executed'] or r['broker_id'] or r['commission'] is not None or r['service_commission'] is not None or r['cancel_requested']):
                raise DataError('NOT_SENT has broker activity')
            if sum(t['qty'] for t in trades)!=r['executed'] or not 0<=r['executed']<=r['qty'] or (r['status']=='FILL' and r['executed']!=r['qty']):
                raise DataError('LIVE execution quantity corrupted')
            for t in trades:
                if t['qty']<1 or number(t['price'],positive=True)<=0 or instant(t['time'])<instant(r['created']):raise DataError('LIVE trade corrupted')
            if r['commission'] is not None and number(r['commission'])<0:raise DataError('Negative broker commission')
        if db.execute('SELECT 1 FROM live_trades t LEFT JOIN live_orders o ON t.request_id=o.request_id WHERE o.request_id IS NULL').fetchone():raise DataError('Orphan LIVE trade')
        if abs(self.position(db))>2:raise DataError('LIVE ledger position limit')

    def position(self,db=None):
        if db is None:
            with self.db() as conn:return self.position(conn)
        return db.execute('SELECT COALESCE(SUM(o.side*t.qty),0) FROM live_trades t JOIN live_orders o USING(request_id)').fetchone()[0]

    def orders(self,active=False):
        with self.db() as db:
            rows=[dict(r) for r in db.execute('SELECT * FROM live_orders ORDER BY rowid')]
        return [r for r in rows if r['status'] not in TERMINAL] if active else rows

    def halt(self,category,now,details=None):
        with self.db() as db:
            db.execute('UPDATE live_meta SET halt=? WHERE id=1',(category,))
            db.execute('INSERT INTO live_events(time,category,details) VALUES(?,?,?)',(iso(now),category,encoded(details).decode() if details else None))

    def snapshot(self,kind,payload,now):
        with self.db() as db:db.execute('INSERT INTO live_snapshots(time,kind,payload) VALUES(?,?,?)',(iso(now),kind,encoded(payload).decode()))

    def ingest(self,rows,now):
        complete={r['provider_day']:r for r in rows if r['is_complete']}
        if not complete:return
        with self.db() as db:
            meta=db.execute('SELECT * FROM live_meta WHERE id=1').fetchone()
            cached=list(db.execute('SELECT * FROM live_bars ORDER BY day'))
            for old in cached:
                if old['day'] not in complete or candle_hash(complete[old['day']])!=old['hash']:
                    raise DataError('LIVE accepted complete candle changed/disappeared')
            if meta['bootstrap'] is None:
                boundary=max(complete);db.execute('UPDATE live_meta SET bootstrap=? WHERE id=1',(boundary,))
            else:boundary=meta['bootstrap']
            hist=[DailyClose(date.fromisoformat(r['day']),float(json.loads(r['payload'])['close'])) for r in cached]
            for day,row in sorted(complete.items()):
                if any(r['day']==day for r in cached):continue
                if cached and day<cached[-1]['day']:raise DataError('LIVE late interior candle insertion')
                if instant(row['time'])>now or instant(row['observed_at'])>now:raise DataError('Future LIVE candle')
                hist.append(DailyClose(date.fromisoformat(day),float(row['close'])))
                db.execute('INSERT INTO live_bars VALUES(?,?,?,?)',(day,encoded(candle_identity(row)).decode(),candle_hash(row),iso(now)))
                target=crossover(hist).target*2 if len(hist)>=51 else 0
                if target and day>boundary:
                    db.execute('INSERT INTO live_signals VALUES(?,?,?,0)',(day,target,iso(now)))

    def signal(self):
        with self.db() as db:
            row=db.execute('SELECT * FROM live_signals ORDER BY day DESC LIMIT 1').fetchone()
            return dict(row) if row and not row['done'] else None

    def done(self,day):
        with self.db() as db:db.execute('UPDATE live_signals SET done=1 WHERE day<=?',(day,))

    def intent(self,signal,phase,side,qty,before,payload,now,context):
        with self.db() as db:
            if db.execute('SELECT 1 FROM live_orders WHERE status NOT IN (\'FILL\',\'CANCELLED\',\'REJECTED\',\'NOT_SENT\')').fetchone():raise DataError('Existing LIVE order')
            if db.execute("SELECT 1 FROM live_orders WHERE status='NOT_SENT'").fetchone():raise DataError('Unsent intent requires explicit review; no replacement')
            db.execute('''INSERT INTO live_orders(request_id,signal_day,phase,side,qty,before_pos,payload,created,status,context)
                VALUES(?,?,?,?,?,?,?,?,'SENDING',?)''',(payload['orderId'],signal['day'],phase,side,qty,before,encoded(payload).decode(),iso(now),encoded(context).decode()))
        # A separate durable highwater prevents silently using an older SQLite
        # backup containing the same ledger UUID but missing posted intents.
        atomic_json(self.path.with_suffix('.highwater.json'),dict(ledger_id=self.meta()['ledger_id'],request_ids=[r['request_id'] for r in self.orders()]))
        if os.name=='posix':
            fd=os.open(self.path.parent,os.O_RDONLY)
            try:os.fsync(fd)
            finally:os.close(fd)

    def ack(self,request_id,response,now):
        status=STATUS.get(response.get('executionReportStatus'))
        if status is None or response.get('orderRequestId')!=request_id:raise DataError('PostOrder acknowledgement identity')
        with self.db() as db:
            if status=='REJECTED' and integer(response.get('lotsExecuted','0'))==0:
                db.execute("UPDATE live_orders SET status='REJECTED',broker_id=? WHERE request_id=?",(response.get('orderId'),request_id))
            else:
                if not response.get('orderId'):raise DataError('Missing broker order ID')
                db.execute("UPDATE live_orders SET status='NEW',broker_id=? WHERE request_id=?",(response['orderId'],request_id))

    def not_sent(self,request_id,now):
        with self.db() as db:
            row=db.execute('SELECT * FROM live_orders WHERE request_id=?',(request_id,)).fetchone()
            if row is None or row['status']!='SENDING' or row['executed'] or row['broker_id'] or row['commission'] is not None or row['service_commission'] is not None or row['cancel_requested'] or db.execute('SELECT 1 FROM live_trades WHERE request_id=?',(request_id,)).fetchone():
                raise DataError('Cannot certify intent as NOT_SENT')
            db.execute("UPDATE live_orders SET status='NOT_SENT' WHERE request_id=?",(request_id,))
            db.execute("UPDATE live_meta SET halt='PRE_HTTP_NOT_SENT_REVIEW' WHERE id=1")
            db.execute("INSERT INTO live_events(time,category) VALUES(?,'PRE_HTTP_NOT_SENT_REVIEW')",(iso(now),))

    def record_order(self,request_id,raw,now):
        status=STATUS.get(raw.get('executionReportStatus'))
        # Preserve the broker's reported cumulative counts even if complete trade
        # stages have not arrived. These are NOT substituted for actual fills.
        self.snapshot('ORDER_REPORT',dict(request_id=request_id,status=status or 'UNRECOGNIZED',
            lots_requested=integer(raw.get('lotsRequested','0')),lots_executed=integer(raw.get('lotsExecuted','0')),
            returned_stage_count=len(raw.get('stages',[])),
            request_and_instrument_match=raw.get('orderRequestId')==request_id and raw.get('instrumentUid')==UID),now)
        with self.db() as db:
            row=db.execute('SELECT * FROM live_orders WHERE request_id=?',(request_id,)).fetchone()
            if row and row['status']=='NOT_SENT':raise DataError('Broker report contradicts NOT_SENT certificate')
            if not row or status is None or raw.get('orderRequestId')!=request_id or raw.get('instrumentUid')!=UID or raw.get('direction')!=('ORDER_DIRECTION_BUY' if row['side']==1 else 'ORDER_DIRECTION_SELL') or raw.get('orderType')!='ORDER_TYPE_LIMIT':
                raise DataError('Broker order identity mismatch')
            qty=integer(raw.get('lotsExecuted','0'))
            if integer(raw['lotsRequested'])!=row['qty'] or not row['executed']<=qty<=row['qty'] or (status=='FILL' and qty!=row['qty']):raise DataError('Broker order quantities inconsistent')
            if row['broker_id'] and row['broker_id']!=raw.get('orderId'):raise DataError('Broker order ID changed')
            stages=raw.get('stages',[])
            if sum(integer(t['quantity']) for t in stages)!=qty or len({t['tradeId'] for t in stages})!=len(stages):
                raise DataError('Broker execution detail incomplete')
            old={r[0] for r in db.execute('SELECT trade_id FROM live_trades WHERE request_id=?',(request_id,))}
            if not old<={t['tradeId'] for t in stages}:raise DataError('Broker execution disappeared')
            context=json.loads(row['context']);outside=False
            for t in stages:
                value=str(quotation(t['price']));stamp=iso(instant(t['executionTime']));quantity=integer(t['quantity'])
                if quantity<1 or number(value,positive=True)<=0 or instant(stamp)<instant(row['created']) or instant(stamp)>now:raise DataError('Broker fill time/price/quantity')
                values=(request_id,stamp,quantity,value,t['price'].get('currency'))
                previous=db.execute('SELECT request_id,time,qty,price,currency FROM live_trades WHERE request_id=? AND trade_id=?',(request_id,t['tradeId'])).fetchone()
                if previous and tuple(previous)!=values:raise DataError('Broker trade identity changed')
                db.execute('INSERT OR IGNORE INTO live_trades VALUES(?,?,?,?,?,?)',(t['tradeId'],*values))
                if not instant(context['window_from'])<=instant(stamp)<instant(context['window_to']):outside=True
            commission=str(rub(raw['executedCommission'])) if 'executedCommission' in raw else None
            service=str(rub(raw['serviceCommission'])) if 'serviceCommission' in raw else None
            if commission is not None and number(commission)<0:raise DataError('Negative commission')
            db.execute('UPDATE live_orders SET broker_id=?,status=?,executed=?,commission=?,service_commission=? WHERE request_id=?',
                       (raw['orderId'],status,qty,commission,service,request_id))
            if abs(self.position(db))>2:raise DataError('Broker fills exceed absolute limit')
            if outside:
                db.execute("UPDATE live_meta SET halt='EXECUTION_OUTSIDE_ALLOWED_WINDOW' WHERE id=1")
                db.execute("INSERT INTO live_events(time,category) VALUES(?,'EXECUTION_OUTSIDE_ALLOWED_WINDOW')",(iso(now),))
