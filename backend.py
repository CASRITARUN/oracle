#!/usr/bin/env python3
"""Option Desk India — fresh implementation, 2026-10-30.
Run: pip install fastapi uvicorn kiteconnect numpy requests python-dotenv
     python backend.py
Keep index.html beside this file. Use ONE process / ONE uvicorn worker.
Reads Kite credentials from environment / .env, with server-side defaults.
Fresh data directory: option_desk_data. No imports from the previous app.
Paper by default. Live entry requires explicit UI arming and server opt-in.
Long CE/PE only. Broker-native SL limits plus independent exit supervision.
Strategy adaptations: github.com/Jakeschincariol/day-trader-skill (MIT).
See README.md for skills mapping, evidence schema, deployment and limitations.
"""
from __future__ import annotations
import asyncio, contextlib, copy, datetime as dt, hashlib, hmac, json, logging, math
import os, queue, re, secrets, sqlite3, threading, time, uuid, xml.etree.ElementTree as ET
from collections import defaultdict, deque
from concurrent.futures import ProcessPoolExecutor
import multiprocessing
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from urllib.parse import urlencode, urlparse
from zoneinfo import ZoneInfo
import numpy as np
import requests
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from kiteconnect import KiteConnect, KiteTicker
from kiteconnect.exceptions import TokenException, InputException, OrderException
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).with_name('.env'))
except ImportError:
    pass

API_KEY = os.environ.get("vecsucwn1tckme31", "").strip()
API_SECRET = os.environ.get("mehksxgc3gsbj3zz7kpacrb9ezrkvzro", "").strip()
# Configure this same redirect URL in the Kite developer app settings.
REDIRECT_URL = os.environ.get("REDIRECT_URL", "https://algo.wecon.in/api/callback")

IST = ZoneInfo('Asia/Kolkata')
ROOT = Path(__file__).resolve().parent
BUILD = 'option-desk-india-1.0.1'
TERMINAL = {'COMPLETE', 'CANCELLED', 'REJECTED'}
INDEX_NAMES = {'NIFTY':'NIFTY 50', 'BANKNIFTY':'NIFTY BANK', 'FINNIFTY':'NIFTY FIN SERVICE',
               'MIDCPNIFTY':'NIFTY MID SELECT', 'NIFTYNXT50':'NIFTY NEXT 50',
               'SENSEX':'SENSEX', 'BANKEX':'BANKEX'}
SOURCE = 'https://github.com/Jakeschincariol/day-trader-skill'
LOG = logging.getLogger('option-desk')

def now(): return dt.datetime.now(IST)
def iso(value=None): return (value or now()).isoformat()
def stamp(value):
    if isinstance(value, dt.datetime): return value.replace(tzinfo=IST) if value.tzinfo is None else value.astimezone(IST)
    if isinstance(value, (int,float)): return dt.datetime.fromtimestamp(value, IST)
    value = dt.datetime.fromisoformat(str(value).replace('Z','+00:00'))
    return value.replace(tzinfo=IST) if value.tzinfo is None else value.astimezone(IST)
def jdump(v): return json.dumps(v, default=str, separators=(',', ':'), allow_nan=False)
def digest(v): return hashlib.sha256(jdump(v).encode()).hexdigest()[:24]
def finite(v, default=0.):
    try:
        x=float(v)
        return x if math.isfinite(x) else default
    except (TypeError,ValueError): return default
def rounded(v, tick=.05, up=False):
    return round((math.ceil(v/tick-1e-9) if up else math.floor(v/tick+1e-9))*tick, 6)
def age(v, t=None):
    try: return ((t or now())-stamp(v)).total_seconds()
    except (TypeError,ValueError): return float('inf')
def safe_error(exc):
    # Do not return URLs, API keys, request/access tokens or arbitrary broker payloads.
    name=type(exc).__name__
    if isinstance(exc, TokenException): return 'Kite session expired. Sign in again.'
    if isinstance(exc, requests.Timeout): return 'Kite request timed out; retry scheduled.'
    if isinstance(exc, requests.ConnectionError): return 'Kite network unavailable; retry scheduled.'
    if isinstance(exc, (ValueError, RuntimeError)): return str(exc)[:220]
    if isinstance(exc, (InputException, OrderException)):
        message=str(exc)
        for key in ['KITE_API_KEY','KITE_API_SECRET','KITE_ACCESS_TOKEN']:
            secret={'KITE_API_KEY':API_KEY,'KITE_API_SECRET':API_SECRET}.get(key,os.getenv(key,''))
            if secret:message=message.replace(secret,'[redacted]')
        message=re.sub(r'https?://\S+','[URL]',message)
        message=re.sub(r'(?i)(access_token|request_token|api_secret|api_key)[=: ]+\S+',r'\1=[redacted]',message)
        return name+': '+message[:220]
    return name + ': operation failed; see service log.'

@dataclass
class Config:
    capital: float = 300000
    cash_buffer_pct: float = 10
    risk_per_trade: float = 1500
    daily_loss: float = 5000
    max_premium: float = 75000
    max_positions: int = 3
    max_same_direction: int = 2
    max_trades: int = 12
    reward_risk: float = 2
    max_spread_pct: float = 2.5
    max_quote_age: float = 8
    max_candle_age: float = 420
    volume_multiple: float = 1
    min_score: float = 55
    entry_after: str = '09:35'
    entry_before: str = '14:50'
    flat_by: str = '15:15'
    fee_bps: float = 15
    slippage_bps: float = 8
    brokerage: float = 20
    require_simulation: bool = False
    require_context: bool = False
    max_correlation: float = .80
    direction_premium_cap: float = 150000
    holidays: list = None
    special_sessions: dict = None
    sectors: dict = None
    watchlist: list = None
    def __post_init__(self):
        for k,default in [('holidays',[]),('special_sessions',{}),('sectors',{}),('watchlist',[])]:
            if getattr(self,k) is None: setattr(self,k,copy.deepcopy(default))
    @classmethod
    def parse(cls, data):
        allowed={f.name for f in fields(cls)}
        if set(data)-allowed: raise ValueError('Unknown settings: '+', '.join(sorted(set(data)-allowed)))
        c=cls(**data)
        bounds={'capital':(1000,1e8),'cash_buffer_pct':(0,95),'risk_per_trade':(10,1e6),
          'daily_loss':(10,1e7),'max_premium':(100,1e7),'max_positions':(1,20),
          'max_same_direction':(1,20),'max_trades':(1,100),'reward_risk':(1,5),
          'max_spread_pct':(.1,10),'max_quote_age':(1,15),'max_candle_age':(300,600),
          'volume_multiple':(0,5),'min_score':(0,100),'fee_bps':(0,100),
          'slippage_bps':(0,100),'brokerage':(0,100),'max_correlation':(.1,1),
          'direction_premium_cap':(100,1e8)}
        for k,(lo,hi) in bounds.items():
            v=getattr(c,k)
            if isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) or not lo<=v<=hi:
                raise ValueError(f'Invalid {k}; expected {lo} to {hi}.')
        for k in ['max_positions','max_same_direction','max_trades']:
            if int(getattr(c,k))!=getattr(c,k): raise ValueError(k+' must be a whole number')
            setattr(c,k,int(getattr(c,k)))
        for k in ['require_simulation','require_context']:
            if not isinstance(getattr(c,k),bool): raise ValueError(k+' must be true/false')
        for k in ['entry_after','entry_before','flat_by']:
            if not isinstance(getattr(c,k),str) or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d',getattr(c,k)): raise ValueError('Invalid time')
        if not '09:15'<=c.entry_after<c.entry_before<c.flat_by<='15:25': raise ValueError('Entry/exit windows must be ordered inside the regular session')
        if not isinstance(c.holidays,list) or not isinstance(c.watchlist,list) or not isinstance(c.sectors,dict) or not isinstance(c.special_sessions,dict): raise ValueError('Invalid calendar/universe settings')
        for x in c.holidays: dt.date.fromisoformat(x)
        for d,v in c.special_sessions.items():
            dt.date.fromisoformat(d)
            if set(v)!={'open','close'} or not re.fullmatch(r'\d\d:\d\d',v['open']) or not re.fullmatch(r'\d\d:\d\d',v['close']) or v['open']>=v['close']: raise ValueError('Invalid special session')
        return c

def session(t, cfg):
    key=t.date().isoformat(); special=cfg.special_sessions.get(key)
    if special: op,cl=special['open'],special['close']
    elif t.weekday()>=5 or key in cfg.holidays: return {'open':False,'entry':False,'label':'Market closed','end':'15:30'}
    else: op,cl='09:15','15:30'
    hh=t.strftime('%H:%M')
    return {'open':op<=hh<cl,'entry':max(op,cfg.entry_after)<=hh<min(cl,cfg.entry_before),
            'label':'Regular session' if not special else 'Special session','end':cl}

def clean_bars(rows, t):
    out={}
    for row in rows:
        try:
            ts=stamp(row.get('date',row.get('ts')))
            if ts.second or ts.microsecond or ts.minute%5 or ts+dt.timedelta(minutes=5)>t: continue
            if not '09:15'<=ts.strftime('%H:%M')<'15:30': continue
            b={'ts':iso(ts),**{k:finite(row.get(k)) for k in ['open','high','low','close','volume']}}
            if min(b['open'],b['low'],b['close'])<=0 or b['high']<max(b['open'],b['close'],b['low']) or b['low']>min(b['open'],b['close']): continue
            out[b['ts']]=b
        except (TypeError,ValueError): continue
    return sorted(out.values(),key=lambda b:b['ts'])

def atr(bars):
    values=[]
    for i,b in enumerate(bars):
        prev=bars[i-1]['close'] if i else b['open']
        values.append(max(b['high']-b['low'],abs(b['high']-prev),abs(b['low']-prev)))
    return float(np.mean(values[-14:])) if values else 0

def signal_for(symbol, bars, t, cfg, is_index=False):
    """Same completed-bar ORB rule in both current scanning and historical replay."""
    rows=clean_bars(bars,t); today=[b for b in rows if stamp(b['ts']).date()==t.date()]
    base={'symbol':symbol,'kind':'INDEX' if is_index else 'STOCK','status':'WAIT','score':0,
          'reason':'Waiting for the first 15-minute opening range','asof':iso(t),'tags':[]}
    if not today: return {**base,'reason':'Waiting for current-session candles'}
    base['bar_time']=today[-1]['ts']; base['spot']=today[-1]['close']
    if age(today[-1]['ts'],t)>cfg.max_candle_age: return {**base,'reason':'Stale completed candles'}
    # Reject missing opening/intermediate bars; never silently fill gaps.
    start=t.replace(hour=9,minute=15,second=0,microsecond=0)
    expected=[iso(start+dt.timedelta(minutes=5*i)) for i in range(len(today))]
    if [b['ts'] for b in today]!=expected: return {**base,'reason':'Candle gap; backfill requested'}
    if len(today)<4: return base
    hi=max(b['high'] for b in today[:3]); lo=min(b['low'] for b in today[:3]); midpoint=(hi+lo)/2
    trigger=None; side=None
    avgvol=float(np.mean([b['volume'] for b in today[:3]]))
    for i,b in enumerate(today[3:],3):
        if not is_index and cfg.volume_multiple and (avgvol<=0 or b['volume']<avgvol*cfg.volume_multiple): continue
        if b['close']>hi: trigger=i;side='CE';break
        if b['close']<lo: trigger=i;side='PE';break
    if trigger is None: return {**base,'reason':'Waiting for a closed-bar range breakout','range_high':hi,'range_low':lo}
    b=today[trigger]
    if trigger!=len(today)-1: return {**base,'reason':'First breakout has passed; no late chase','direction':side}
    history=[x for x in rows if x['ts']<=b['ts']]
    a=atr(history); distance=max(abs(b['close']-midpoint),a*1.2)
    if distance<=0: return {**base,'reason':'Invalid volatility estimate'}
    direction=1 if side=='CE' else -1
    trend=float(np.mean([x['close'] for x in history[-20:]]))
    volume_x=b['volume']/avgvol if avgvol else None
    score=55+min(12,abs(b['close']-(hi if direction>0 else lo))/max(a,.001)*8)
    score+=8 if direction*(b['close']-trend)>0 else 0
    if not is_index and volume_x: score+=min(8,max(0,volume_x-1)*4)
    # Daily context uses complete prior sessions only.
    daily={}
    for x in rows:
        day=stamp(x['ts']).date()
        if day<t.date(): daily[day]=x['close']
    closes=list(daily.values()); tags=['Opening-range breakout' if direction>0 else 'Opening-range breakdown']
    if len(closes)>=21 and closes[-1]>max(closes[-21:-1]): tags.append('20-session closing high')
    if len(closes)>=16 and closes[-6]/closes[-16]>1.10 and max(closes[-5:])/min(closes[-5:])<1.05: tags.append('Bull flag')
    if len(closes)>=21 and closes[-2]<np.mean(closes[-21:-1]) and closes[-1]>np.mean(closes[-20:]): tags.append('MA20 reclaim')
    if len(closes)>=15:
        diff=np.diff(closes[-15:]); gain=np.maximum(diff,0).mean(); loss=np.maximum(-diff,0).mean()
        rsi=100-100/(1+gain/loss) if loss else 100
        if rsi<30: tags.append('Oversold')
    return {**base,'status':'SETUP','reason':'Closed-bar trigger confirmed','direction':side,'score':round(score,1),
       'trigger':b['ts'],'spot':b['close'],'underlying_stop':b['close']-direction*distance,
       'underlying_target':b['close']+direction*cfg.reward_risk*distance,'atr':a,'risk_distance':distance,
       'range_high':hi,'range_low':lo,'volume_x':volume_x,'tags':tags,
       'fingerprint':digest([symbol,b['ts'],side,'orb-v1'])}

def norm_cdf(x):
    # Vectorised Abramowitz-Stegun approximation; max error approx 7.5e-8.
    x=np.asarray(x,dtype=float); a=np.abs(x); k=1/(1+.2316419*a)
    p=1-np.exp(-a*a/2)/math.sqrt(2*math.pi)*k*(.319381530+k*(-.356563782+k*(1.781477937+k*(-1.821255978+k*1.330274429))))
    return np.where(x>=0,p,1-p)

def bs(spot,strike,years,vol,side):
    s=np.maximum(np.asarray(spot,dtype=float),.001); t=max(years,1/(365*24*60)); v=max(vol,.01)
    d1=(np.log(s/strike)+(.06+.5*v*v)*t)/(v*math.sqrt(t));d2=d1-v*math.sqrt(t)
    call=s*norm_cdf(d1)-strike*math.exp(-.06*t)*norm_cdf(d2)
    return np.maximum(.001,call if side=='CE' else call-s+strike*math.exp(-.06*t))

def implied_vol(premium,spot,strike,years,side):
    lo,hi=.01,3.
    if not float(bs(spot,strike,years,lo,side))<=premium<=float(bs(spot,strike,years,hi,side)): return None
    for _ in range(45):
        mid=(lo+hi)/2
        if float(bs(spot,strike,years,mid,side))>premium: hi=mid
        else: lo=mid
    return (lo+hi)/2

def full_sessions(bars, before):
    days=defaultdict(list)
    for b in clean_bars(bars,before):
        day=stamp(b['ts']).date()
        if day<before.date(): days[day].append(b)
    out=[]
    for day,rows in sorted(days.items()):
        start=dt.datetime.combine(day,dt.time(9,15),IST)
        if len(rows)==75 and all(stamp(b['ts'])==start+dt.timedelta(minutes=5*i) for i,b in enumerate(rows)): out.append(rows)
    return out[-60:]

def replay_job(bars,plan,config):
    """CPU-only process job; immutable plan and prior full sessions, never broker calls."""
    cfg=Config.parse(config); t=stamp(plan['trigger'])+dt.timedelta(minutes=5)
    days=full_sessions(bars,t); result={'plan_id':plan['plan_id'],'sessions':len(days),'seed':19,'paths':0,
       'status':'INSUFFICIENT','note':'At least 20 full prior sessions required; no invented history.'}
    if len(days)<20: return result
    start=(t.hour*60+t.minute-(9*60+15))//5
    end=(int(cfg.flat_by[:2])*60+int(cfg.flat_by[3:])-(9*60+15))//5
    end=min(75,end)
    if start>=end: return result
    ratios=[]
    for rows in days:
        ratios.append([[b[k]/(rows[i-1]['close'] if i else b['open']) for k in ['open','high','low','close']] for i,b in enumerate(rows)])
    pool=np.array(ratios); rng=np.random.default_rng(19); n=10000
    prices=np.full(n,plan['spot']); outcome=np.zeros(n,dtype=int); rr=np.zeros(n); fan=[]
    expiry=stamp(plan['expiry']+'T15:30:00+05:30'); years=max((expiry-t).total_seconds()/31536000,1e-6)
    risk=plan['entry']-plan['stop']; scale=plan['entry']/float(bs(plan['spot'],plan['strike'],years,plan['iv'],plan['direction']))
    for step in range(start,end):
        day_ix=rng.integers(0,len(days),n); bar_ix=rng.integers(max(1,step-2),min(75,step+3),n)
        ohlc=pool[day_ix,bar_ix]*prices[:,None]; prices=ohlc[:,3]
        remaining=max(years-(step-start+1)*300/31536000,1e-6)
        premium=bs(ohlc,plan['strike'],remaining,plan['iv'],plan['direction'])*scale
        low=np.min(premium,axis=1); high=np.max(premium,axis=1); opening=premium[:,0]
        stop=(outcome==0)&(low<=plan['stop']); target=(outcome==0)&~stop&(high>=plan['target'])
        rr[stop]=(np.minimum(opening[stop],plan['stop'])-plan['entry'])/risk
        rr[target]=(plan['target']-plan['entry'])/risk;outcome[stop]=-1;outcome[target]=1
        if (step-start)%3==0 or step==end-1:
            fan.append({'minute':step*5+9*60+15+5,'p10':float(np.percentile(premium[:,3],10)),
                        'p50':float(np.percentile(premium[:,3],50)),'p90':float(np.percentile(premium[:,3],90))})
    remaining=(outcome==0);rr[remaining]=(premium[remaining,3]-plan['entry'])/risk
    rr-=plan.get('cost_per_unit',0)/risk
    historical=[]
    for rows in days:
        for i in range(3,min(end,len(rows)-1)):
            clock=stamp(rows[i]['ts'])+dt.timedelta(minutes=5)
            s=signal_for(plan['symbol'],rows[:i+1],clock,cfg,plan['kind']=='INDEX')
            if s['status']!='SETUP' or s['direction']!=plan['direction']: continue
            distance=s['risk_distance'];direction=1 if s['direction']=='CE' else -1; value=0
            for b in rows[i+1:end]:
                adverse=b['low'] if direction>0 else b['high'];favorable=b['high'] if direction>0 else b['low']
                if direction*(adverse-s['underlying_stop'])<=0:
                    value=min(-1,direction*(b['open']-s['spot'])/distance);break
                if direction*(favorable-s['underlying_target'])>=0: value=cfg.reward_risk;break
                value=direction*(b['close']-s['spot'])/distance
            historical.append(value);break
    scenarios=[]
    for factor in [.75,1,1.25]:
        mark=bs(prices,plan['strike'],remaining_years:=max(years-(end-start)*300/31536000,1e-6),plan['iv']*factor,plan['direction'])*scale
        scenarios.append({'iv_change_pct':round((factor-1)*100),'median_premium':round(float(np.median(mark)),2)})
    return {**result,'status':'READY','paths':n,'target_first':float(np.mean(outcome==1)),
      'stop_first':float(np.mean(outcome==-1)),'neither':float(np.mean(outcome==0)),
      'mean_r_after_estimated_costs':float(np.mean(rr)),'fan':fan,'scenarios':scenarios,
      'history_occurrences':len(historical),'history_underlying_mean_r':float(np.mean(historical)) if historical else None,
      'note':'Option scenarios use fixed fitted IV and Black–Scholes; historical replay is on the underlying, not actual option fills.'}

class Store:
    """Short WAL transactions. Network / analysis never run inside a transaction."""
    def __init__(self,path):
        self.path=str(path)
        with self.connection() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS candles(token INTEGER NOT NULL,ts TEXT NOT NULL,data TEXT NOT NULL,PRIMARY KEY(token,ts));
            CREATE TABLE IF NOT EXISTS orders(id TEXT PRIMARY KEY,tag TEXT UNIQUE NOT NULL,mode TEXT NOT NULL,
              kind TEXT NOT NULL,position_id TEXT NOT NULL,fingerprint TEXT,plan TEXT NOT NULL,side TEXT NOT NULL,
              qty INTEGER NOT NULL,price REAL NOT NULL,trigger REAL NOT NULL DEFAULT 0,status TEXT NOT NULL,
              broker_id TEXT UNIQUE,filled INTEGER NOT NULL DEFAULT 0,notional REAL NOT NULL DEFAULT 0,
              avg REAL NOT NULL DEFAULT 0,created TEXT NOT NULL,updated TEXT NOT NULL,note TEXT NOT NULL DEFAULT '',
              cancel_at TEXT,quote_stamp TEXT,UNIQUE(mode,fingerprint));
            CREATE TABLE IF NOT EXISTS positions(id TEXT PRIMARY KEY,mode TEXT NOT NULL,plan TEXT NOT NULL,
              qty INTEGER NOT NULL,entry REAL NOT NULL,opened TEXT NOT NULL,exit_reason TEXT NOT NULL DEFAULT '');
            CREATE TABLE IF NOT EXISTS fills(id INTEGER PRIMARY KEY AUTOINCREMENT,order_id TEXT NOT NULL,
              mode TEXT NOT NULL,side TEXT NOT NULL,qty INTEGER NOT NULL,price REAL NOT NULL,cost REAL NOT NULL,
              pnl REAL NOT NULL,ts TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT,ts TEXT NOT NULL,
              level TEXT NOT NULL,kind TEXT NOT NULL,message TEXT NOT NULL,once_key TEXT UNIQUE);
            PRAGMA user_version=1;
            ''')
    @contextlib.contextmanager
    def connection(self,write=False):
        db=sqlite3.connect(self.path,timeout=2,isolation_level=None);db.row_factory=sqlite3.Row
        db.execute('PRAGMA journal_mode=WAL');db.execute('PRAGMA busy_timeout=2000');db.execute('PRAGMA synchronous=FULL')
        try:
            if write: db.execute('BEGIN IMMEDIATE')
            yield db
            if write: db.commit()
        except Exception:
            if write: db.rollback()
            raise
        finally: db.close()
    def get(self,key,default=None):
        with self.connection() as db: row=db.execute('SELECT value FROM settings WHERE key=?',(key,)).fetchone()
        return json.loads(row[0]) if row else default
    def set(self,key,value):
        with self.connection(True) as db: db.execute('INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(key,jdump(value)))
    def event(self,level,kind,message,once=None):
        with self.connection(True) as db:
            db.execute('INSERT OR IGNORE INTO events(ts,level,kind,message,once_key) VALUES(?,?,?,?,?)',(iso(),level,kind,str(message)[:700],once))
    def events(self):
        with self.connection() as db: return [dict(x) for x in db.execute('SELECT * FROM events ORDER BY id DESC LIMIT 60')]
    def bars(self,token):
        with self.connection() as db: rows=db.execute('SELECT data FROM candles WHERE token=? ORDER BY ts DESC LIMIT 5500',(int(token),)).fetchall()
        return list(reversed([json.loads(x[0]) for x in rows]))
    def put_bars(self,token,bars):
        with self.connection(True) as db:
            db.executemany('INSERT INTO candles(token,ts,data) VALUES(?,?,?) ON CONFLICT(token,ts) DO UPDATE SET data=excluded.data',[(int(token),b['ts'],jdump(b)) for b in bars])
            db.execute('DELETE FROM candles WHERE token=? AND ts<?',(int(token),iso(now()-dt.timedelta(days=95))))
    def orders(self,active=False,mode=None):
        sql='SELECT * FROM orders WHERE 1=1';args=[]
        if active: sql+=" AND status NOT IN ('COMPLETE','CANCELLED','REJECTED')"
        if mode: sql+=' AND mode=?';args.append(mode)
        sql+=' ORDER BY created DESC'
        if not active: sql+=' LIMIT 200'
        with self.connection() as db: rows=[dict(x) for x in db.execute(sql,args)]
        for r in rows:r['plan']=json.loads(r['plan'])
        return rows
    def positions(self,mode=None):
        with self.connection() as db:
            rows=[dict(x) for x in db.execute('SELECT * FROM positions WHERE qty>0'+(' AND mode=?' if mode else ''),(mode,) if mode else ())]
        for r in rows:r['plan']=json.loads(r['plan'])
        return rows
    def mark_exit(self,pid,reason):
        with self.connection(True) as db: db.execute("UPDATE positions SET exit_reason=? WHERE id=? AND qty>0",(reason,pid))
    def update_order(self,oid,**kw):
        allowed={'status','broker_id','note','cancel_at','quote_stamp'}
        if not set(kw)<=allowed:raise ValueError('Invalid order update')
        kw['updated']=iso()
        with self.connection(True) as db:
            old=db.execute('SELECT status FROM orders WHERE id=?',(oid,)).fetchone()
            if old and old['status'] in TERMINAL and kw.get('status') not in TERMINAL:kw.pop('status',None)
            db.execute('UPDATE orders SET '+','.join(k+'=?' for k in kw)+' WHERE id=?',(*kw.values(),oid))
    def create_order(self,mode,kind,plan,side,qty,price,pid=None):
        if qty<=0:raise ValueError('Order quantity must be positive')
        oid=uuid.uuid4().hex;pid=pid or oid;tag='OD'+oid[:18]
        fp=plan['fingerprint'] if kind=='entry' else None
        with self.connection(True) as db:
            if kind!='entry':
                if db.execute("SELECT 1 FROM orders WHERE position_id=? AND side='SELL' AND status NOT IN ('COMPLETE','CANCELLED','REJECTED')",(pid,)).fetchone():return None
                pos=db.execute('SELECT qty FROM positions WHERE id=?',(pid,)).fetchone()
                if not pos or qty>pos['qty']:raise ValueError('Exit exceeds owned quantity')
            try:
                db.execute('''INSERT INTO orders(id,tag,mode,kind,position_id,fingerprint,plan,side,qty,price,trigger,status,created,updated)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',(oid,tag,mode,kind,pid,fp,jdump(plan),side,qty,price,plan['stop'] if kind=='protect' else 0,'CREATED',iso(),iso()))
            except sqlite3.IntegrityError:return None
        self.event('info','order',f'{mode.upper()} {side} {qty} {plan["contract"]} · {kind}',oid)
        return oid
    def claim(self,oid):
        with self.connection(True) as db:
            return db.execute("UPDATE orders SET status='SUBMITTING',updated=? WHERE id=? AND status='CREATED'",(iso(),oid)).rowcount==1
    def apply_report(self,oid,filled,average,status,cfg):
        """Cumulative broker fills -> incremental ledger; duplicates cannot double count."""
        with self.connection(True) as db:
            row=db.execute('SELECT * FROM orders WHERE id=?',(oid,)).fetchone()
            if not row:raise ValueError('Unknown local order')
            o=dict(row);plan=json.loads(o['plan']);filled=int(filled);average=finite(average)
            if filled<o['filled']:return # Ignore reordered stale reports.
            if filled>o['qty'] or (filled and average<=0):raise ValueError('Invalid broker fill report')
            if o['status'] in TERMINAL and filled==o['filled']:return
            extra=filled-o['filled']; total=filled*average;increment=total-o['notional']
            if extra:
                if increment<=0:raise ValueError('Invalid cumulative fill value')
                px=increment/extra;cost=increment*cfg.fee_bps/10000+(cfg.brokerage if o['filled']==0 else 0)
                p=db.execute('SELECT * FROM positions WHERE id=?',(o['position_id'],)).fetchone()
                if o['side']=='BUY':
                    qty=(p['qty'] if p else 0)+extra;entry=((p['qty']*p['entry'] if p else 0)+increment)/qty;pnl=-cost
                    db.execute('''INSERT INTO positions(id,mode,plan,qty,entry,opened) VALUES(?,?,?,?,?,?)
                      ON CONFLICT(id) DO UPDATE SET qty=excluded.qty,entry=excluded.entry''',
                      (o['position_id'],o['mode'],jdump(plan),qty,entry,iso()))
                else:
                    if not p or extra>p['qty']:raise ValueError('Broker fill exceeds locally owned quantity; manual reconciliation required')
                    pnl=(px-p['entry'])*extra-cost
                    db.execute('UPDATE positions SET qty=qty-? WHERE id=?',(extra,o['position_id']))
                db.execute('INSERT INTO fills(order_id,mode,side,qty,price,cost,pnl,ts) VALUES(?,?,?,?,?,?,?,?)',(oid,o['mode'],o['side'],extra,px,cost,pnl,iso()))
            db.execute('UPDATE orders SET filled=?,avg=?,notional=?,status=?,updated=? WHERE id=?',(filled,average,total,status,iso(),oid))
    def totals(self,mode,t=None):
        day=(t or now()).date().isoformat()
        with self.connection() as db:
            r=db.execute('SELECT COALESCE(SUM(pnl),0) pnl,COALESCE(SUM(cost),0) costs FROM fills WHERE mode=? AND substr(ts,1,10)=?',(mode,day)).fetchone()
            n=db.execute("SELECT count(*) FROM orders WHERE mode=? AND kind='entry' AND substr(created,1,10)=? AND (filled>0 OR status NOT IN ('REJECTED','CANCELLED'))",(mode,day)).fetchone()[0]
            all_pnl=db.execute('SELECT COALESCE(SUM(pnl),0) FROM fills WHERE mode=?',(mode,)).fetchone()[0]
        return {'realised':r['pnl'],'costs':r['costs'],'trades':n,'all_realised':all_pnl}

class Pace:
    def __init__(self,seconds):self.seconds=seconds;self.lock=threading.Lock();self.next=0
    def wait(self):
        with self.lock:
            delay=max(0,self.next-time.monotonic());self.next=max(self.next,time.monotonic())+self.seconds
        if delay:time.sleep(delay)

class Broker:
    """Thread-local HTTP clients with bounded timeouts; never blindly retry a write."""
    def __init__(self,folder):
        self.key=API_KEY.strip();self.secret=API_SECRET.strip()
        self.folder=folder;self.local=threading.local();self.token='';self.generation=0
        self.status='Login required';self.user='';self.ticker=None;self.feed='Disconnected';self.on_ticks=None
        self.limits={'history':Pace(.55),'quotes':Pace(1.1),'account':Pace(.35),'write':Pace(.4),'master':Pace(1)}
        self.tokens=set();self.token_map={};self.reconnect_at=0;self.last_tick=0
        p=folder/'kite_session.json'
        if p.exists():
            try:
                data=json.loads(p.read_text())
                if stamp(data['expires'])>now():self.token=data['access_token'];self.user=data.get('user_id','');self.status='Checking session'
            except (ValueError,KeyError):pass
    def client(self):
        if getattr(self.local,'generation',-1)!=self.generation:
            self.local.client=KiteConnect(api_key=self.key,access_token=self.token or None,timeout=6)
            self.local.generation=self.generation
        return self.local.client
    def call(self,lane,method,*args,**kwargs):
        if not self.key or not self.token:raise RuntimeError('Kite login required')
        self.limits[lane].wait()
        try:return getattr(self.client(),method)(*args,**kwargs)
        except TokenException:
            self.status='Login required';self.token='';self.generation+=1
            if self.ticker:
                from twisted.internet import reactor
                reactor.callFromThread(self.ticker.close)
            raise
    def login_url(self):
        if not self.key or not self.secret:raise ValueError('Set KITE_API_KEY and KITE_API_SECRET on the server first')
        return KiteConnect(api_key=self.key).login_url()
    def login(self,request_token):
        data=KiteConnect(api_key=self.key,timeout=6).generate_session(request_token,self.secret)
        expected=os.getenv('KITE_USER_ID','')
        if expected and expected!=data.get('user_id'):raise ValueError('Signed-in Kite account differs from KITE_USER_ID')
        self.token=data['access_token'];self.user=data.get('user_id','');self.generation+=1;self.status='Connected'
        expiry=now().replace(hour=6,minute=0,second=0,microsecond=0)
        if expiry<=now():expiry+=dt.timedelta(days=1)
        p=self.folder/'kite_session.json';tmp=p.with_suffix('.tmp')
        fd=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
        with os.fdopen(fd,'w') as f:json.dump({'access_token':self.token,'user_id':self.user,'expires':iso(expiry)},f)
        os.replace(tmp,p)
        self.restart_stream()
    def restart_stream(self):
        if self.ticker:
            from twisted.internet import reactor
            reactor.callFromThread(self.ticker.close)
        self.ticker=None;self.reconnect_at=0
    def sync_stream(self,tokens):
        self.tokens=set(int(x) for x in tokens)
        if not self.token:return
        if not self.ticker and time.monotonic()>=self.reconnect_at:
            self.reconnect_at=time.monotonic()+60
            ticker=KiteTicker(self.key,self.token,reconnect=True,reconnect_max_tries=50,reconnect_max_delay=30,connect_timeout=8)
            self.ticker=ticker
            def connected(ws,response):
                self.feed='Streaming';wanted=list(self.tokens)
                if wanted:ws.subscribe(wanted);ws.set_mode(ws.MODE_FULL,wanted)
            def ticks(ws,rows):
                self.last_tick=time.monotonic();self.feed='Streaming'
                if self.on_ticks:self.on_ticks(rows)
            ticker.on_connect=connected;ticker.on_ticks=ticks
            ticker.on_close=lambda ws,code,reason:setattr(self,'feed','Reconnecting; REST fallback')
            ticker.on_error=lambda ws,code,reason:setattr(self,'feed','Feed error; REST fallback')
            ticker.on_reconnect=lambda ws,attempt:setattr(self,'feed',f'Reconnecting ({attempt}); REST fallback')
            ticker.on_noreconnect=lambda ws:setattr(self,'ticker',None)
            ticker.connect(threaded=True)
        elif self.ticker and self.ticker.is_connected():
            from twisted.internet import reactor
            ticker=self.ticker
            def change():
                old=set(ticker.subscribed_tokens);add=list(self.tokens-old);remove=list(old-self.tokens)
                if remove:ticker.unsubscribe(remove)
                if add:ticker.subscribe(add);ticker.set_mode(ticker.MODE_FULL,add)
            reactor.callFromThread(change)
    def place(self,o):
        p=o['plan']
        return self.call('write','place_order',variety='regular',exchange=p['exchange'],tradingsymbol=p['contract'],
          transaction_type=o['side'],quantity=o['qty'],product='MIS',order_type='SL' if o['kind']=='protect' else 'LIMIT',
          price=o['price'],trigger_price=o['trigger'] if o['kind']=='protect' else None,validity='DAY',tag=o['tag'])
    def cancel(self,o):return self.call('write','cancel_order',variety='regular',order_id=o['broker_id'])

SKILLS = [
 ('scanner','Indian optionable universe','Kite NFO/BFO contracts; completed underlying candles; ORB and daily context'),
 ('entry','Entry timing','First closed 5-minute breakout/breakdown after 09:15–09:30; no late chase'),
 ('exit','Exit plan','Volatility/structure stop before entry; option tick rounding; target and flat-by time'),
 ('timemachine','Time Machine','10,000 seeded option scenarios plus identical underlying-rule historical replay'),
 ('budget','Lot sizing','Whole exchange lots; stop risk, premium cap, cash buffer and displayed ask depth'),
 ('risk','Risk checks','Fresh data, risk, daily loss, position/direction/sector/correlation and duplicate checks'),
 ('broker','Kite execution','Persistent intents, confirmed cumulative fills, native SL limit, cancellation/reconciliation'),
 ('alerts','Alerts','Deduplicated entry/fill/stop/target/flat-by/feed alerts in the journal'),
 ('news','Indian announcements','Official NSE RSS; source, publication time, matching symbol and event flags'),
 ('analyst','Results analyst','Source-backed imported financial rows: year-on-year growth, margins, TTM when comparable'),
 ('pelosi','Public disclosures','Adapted to Indian insider/promoter disclosures with trade/publication lag; never copied trades')]

class Engine:
    def __init__(self,folder,broker=None):
        self.folder=Path(folder);self.folder.mkdir(parents=True,exist_ok=True)
        self.db=Store(self.folder/'option_desk.sqlite3');self.cfg=Config.parse(self.db.get('config',asdict(Config())))
        self.broker=broker or Broker(self.folder);self.broker.on_ticks=self.on_ticks
        self.lock=threading.RLock();self.control=threading.RLock();self.stop=threading.Event();self.refresh=threading.Event()
        self.mode='paper';self.armed=False;self.arm_day='';self.jobs=queue.Queue(maxsize=100)
        self.quotes={};self.universe={};self.options=defaultdict(list);self.instruments={};self.bars={};self.candle_status={}
        self.candidates=[];self.plans={};self.replays={};self.futures={};self.pool=None
        self.master_date='';self.master_due=0;self.cursor=0;self.health={};self.snapshot={'build':BUILD,'ready':False}
        self.account={'checked':None,'cash':None,'external':[],'error':'Broker not connected'}
        self.context=self.db.get('context',{'events':[],'financials':[],'disclosures':[]})
        self.context_health={'status':'No feed read yet','updated':None};self.feed_events=[]
        self.halt='';self.paper_halt_day='';self.live_halt_day='';self.threads=[]
        self.load_master()
        # A previous process may have died between durable intent and HTTP response.
        for o in self.db.orders(active=True):
            if o['status']=='SUBMITTING':self.db.update_order(o['id'],status='UNKNOWN',note='Process restarted during submission; reconcile before any retry')
        self.db.event('info','startup','Fresh engine started disarmed. Existing positions continue to be supervised.')
    def heartbeat(self,name,status='ok',detail=''):
        with self.lock:self.health[name]={'at':iso(),'status':status,'detail':detail}
    def loop(self,name,fn,interval):
        failures=0
        while not self.stop.is_set():
            begun=time.monotonic()
            try:
                fn();failures=0;self.heartbeat(name)
            except Exception as exc:
                failures+=1;msg=safe_error(exc);self.heartbeat(name,'error',msg)
                self.db.event('error',name,msg,once=f'{name}:{now().date()}:{type(exc).__name__}:{int(time.time()//300)}')
                LOG.warning('%s: %s',name,type(exc).__name__)
            delay=max(.05,interval-(time.monotonic()-begun))
            if failures:delay=max(delay,min(30,2**min(failures,5)))
            self.stop.wait(delay)
    def start(self):
        self.pool=ProcessPoolExecutor(max_workers=1,mp_context=multiprocessing.get_context("spawn"))
        for name,fn,every in [('catalog',self.catalog_tick,15),('quotes',self.quote_tick,1.2),
          ('candles',self.candle_tick,.6),('analysis',self.analysis_tick,1.5),('broker',self.broker_tick,1),
          ('reconciliation',self.reconciliation_tick,1.5),('supervision',self.supervise_tick,.5),('context',self.context_tick,180),('snapshot',self.publish,.5)]:
            thread=threading.Thread(target=self.loop,args=(name,fn,every),name=name,daemon=True);thread.start();self.threads.append(thread)
    def close(self):
        self.stop.set();self.armed=False
        if self.pool:self.pool.shutdown(wait=False,cancel_futures=True)
        if self.broker.ticker:
            from twisted.internet import reactor
            reactor.callFromThread(self.broker.ticker.close)
    def load_master(self):
        p=self.folder/'instruments.json'
        if p.exists():
            try:
                data=json.loads(p.read_text());self.install_master(data['rows'],data['date'])
            except (ValueError,KeyError):pass
    def install_master(self,rows,date):
        instruments={int(x['instrument_token']):x for x in rows}
        options=defaultdict(list)
        for x in rows:
            if x.get('instrument_type') in ('CE','PE') and x.get('exchange') in ('NFO','BFO'):
                if str(x.get('expiry',''))[:10]>=now().date().isoformat() and int(x.get('lot_size',0))>0:options[x['name']].append(x)
        universe={}
        for symbol,contracts in options.items():
            index=symbol in INDEX_NAMES;target=INDEX_NAMES.get(symbol,symbol)
            cash_exchange='BSE' if contracts[0]['exchange']=='BFO' else 'NSE'
            matches=[x for x in rows if x.get('exchange')==cash_exchange and x.get('tradingsymbol')==target and (index or x.get('instrument_type')=='EQ')]
            if len(matches)==1:
                x=matches[0];universe[symbol]={'symbol':symbol,'token':int(x['instrument_token']),
                  'key':cash_exchange+':'+target,'kind':'INDEX' if index else 'STOCK'}
        if self.cfg.watchlist:universe={k:v for k,v in universe.items() if k in self.cfg.watchlist}
        with self.lock:
            self.instruments=instruments;self.options=options;self.universe=universe;self.master_date=date
        # Load cached bars outside the short in-memory lock.
        for symbol,u in universe.items():
            if symbol not in self.bars:self.bars[symbol]=self.db.bars(u['token'])
    def catalog_tick(self):
        if not self.broker.token:return
        if self.master_date==now().date().isoformat() and self.universe:return
        rows=self.broker.call('master','instruments')
        if not rows:raise RuntimeError('Kite returned an empty instrument master')
        date=now().date().isoformat();self.install_master(rows,date)
        p=self.folder/'instruments.json';tmp=p.with_suffix('.tmp');tmp.write_text(jdump({'date':date,'rows':rows}));os.replace(tmp,p)
    def normal_quote(self,row,source):
        ts=row.get('exchange_timestamp') or row.get('timestamp')
        # Index websocket packets do not have exchange timestamps; receipt time is labelled.
        has_depth=bool(row.get('depth'))
        q={'ltp':finite(row.get('last_price')),'at':iso(stamp(ts)) if ts else None,
           'received':iso(),'source':source,'bid':0.,'ask':0.,'bid_qty':0,'ask_qty':0,
           'volume':finite(row.get('volume_traded',row.get('volume')))}
        if has_depth:
            buys=[x for x in row['depth'].get('buy',[]) if finite(x.get('price'))>0 and int(x.get('quantity',0))>0]
            sells=[x for x in row['depth'].get('sell',[]) if finite(x.get('price'))>0 and int(x.get('quantity',0))>0]
            if buys:q.update(bid=finite(buys[0]['price']),bid_qty=int(buys[0]['quantity']))
            if sells:q.update(ask=finite(sells[0]['price']),ask_qty=int(sells[0]['quantity']))
        return q
    def on_ticks(self,rows):
        updates={int(r['instrument_token']):self.normal_quote(r,'WebSocket') for r in rows}
        with self.lock:self.quotes.update(updates)
    def quote(self,token):
        with self.lock:return copy.copy(self.quotes.get(int(token),{}))
    def quote_ok(self,q,depth=True,t=None):
        if not q or age(q.get('received'),t)>self.cfg.max_quote_age:return False
        if depth and (not q.get('at') or age(q['at'],t)>self.cfg.max_quote_age or not 0<q['bid']<=q['ask']):return False
        return q.get('ltp',0)>0
    def wanted_tokens(self):
        with self.lock:
            tokens={u['token'] for u in self.universe.values()};candidates=list(self.candidates)
        for s in candidates[:30]:
            if not s.get('direction') or not s.get('spot'):continue
            contracts=self.near_contracts(s)
            tokens.update(int(x['instrument_token']) for x in contracts[:4])
        for p in self.db.positions():tokens.add(int(p['plan']['token']))
        for o in self.db.orders(active=True):tokens.add(int(o['plan']['token']))
        return list(tokens)[:2800]
    def quote_tick(self):
        if not self.broker.token:return
        tokens=self.wanted_tokens();self.broker.sync_stream(tokens)
        needed=[]
        for token in tokens:
            q=self.quote(token)
            if age(q.get('received'))>3:needed.append(token)
        if needed:
            raw=self.broker.call('quotes','quote',[str(x) for x in needed[:450]])
            updated={int(row['instrument_token']):self.normal_quote(row,'REST fallback') for row in raw.values() if 'instrument_token' in row}
            with self.lock:self.quotes.update(updated)
    def candle_tick(self):
        if not self.broker.token or not self.universe:return
        names=list(self.universe);self.cursor%=len(names)
        symbol=names[self.cursor];self.cursor=(self.cursor+1)%len(names)
        t=now();previous=self.bars.get(symbol,[]);status=self.candle_status.get(symbol,{})
        refresh=self.refresh.is_set()
        if refresh:self.refresh.clear()
        if not refresh and age(status.get('attempted'))<120:return
        if not session(t,self.cfg)['open'] and age(status.get('success'))<3600:return
        u=self.universe[symbol];start=t-dt.timedelta(days=60)
        if previous:start=max(start,stamp(previous[-1]['ts'])-dt.timedelta(minutes=10))
        self.candle_status[symbol]={**status,'attempted':iso(t),'error':'','state':'Fetching'}
        try:
            raw=self.broker.call('history','historical_data',u['token'],start,t,'5minute',continuous=False,oi=False)
            bars=clean_bars(raw,t)
            if not bars:raise RuntimeError('No completed candles returned')
            self.db.put_bars(u['token'],bars)
            merged={b['ts']:b for b in previous}
            merged.update({b['ts']:b for b in bars});allbars=sorted(merged.values(),key=lambda x:x['ts'])[-5500:]
            with self.lock:self.bars[symbol]=allbars
            self.candle_status[symbol]={'attempted':iso(t),'success':iso(),'latest':bars[-1]['ts'],
              'state':'Fresh' if age(bars[-1]['ts'])<=self.cfg.max_candle_age else 'Stale','error':''}
        except Exception as e:
            self.candle_status[symbol]={**self.candle_status[symbol],'state':'Error','error':safe_error(e)}
            raise
    def near_contracts(self,s):
        t=now();rows=[]
        for x in self.options.get(s['symbol'],[]):
            expiry=str(x.get('expiry',''))[:10]
            # Single-stock physical-settlement exposure: skip expiry-day contracts entirely.
            if expiry<t.date().isoformat() or (s['kind']=='STOCK' and expiry==t.date().isoformat()):continue
            if x.get('instrument_type')!=s['direction']:continue
            rows.append(x)
        rows.sort(key=lambda x:(str(x['expiry'])[:10],abs(finite(x['strike'])-s['spot'])))
        return rows[:6]
    def context_for(self,s):
        sym=s['symbol'];t=now();items=self.context.get('events',[])+self.feed_events
        events=[x for x in items if x['symbol'] in (sym,'*') and 0<=age(x['published_at'],t)<=86400]
        financials=[x for x in self.context.get('financials',[]) if x['symbol']==sym and age(x['published_at'],t)>=0]
        financials.sort(key=lambda x:x['period']);metrics={}
        if financials:
            last=financials[-1];yearago=next((x for x in reversed(financials[:-1]) if x['period'][:4]==str(int(last['period'][:4])-1) and x['period'][4:]==last['period'][4:] and x.get('basis')==last.get('basis')),None)
            revenue=last['revenue'];metrics={'period':last['period'],'source_url':last['source_url'],'basis':last.get('basis',''),
               'revenue':revenue,'net_income':last['net_income'],'net_margin_pct':last['net_income']/revenue*100 if revenue else None,
               'revenue_yoy_pct':(revenue/yearago['revenue']-1)*100 if yearago and yearago['revenue'] else None}
            # TTM only from four non-overlapping quarterly rows on the same reporting basis.
            quarters=[x for x in financials if x.get('period_type')=='quarter' and x.get('basis')==last.get('basis')][-4:]
            if len(quarters)==4 and all(70<=(dt.date.fromisoformat(b['period'])-dt.date.fromisoformat(a['period'])).days<=110 for a,b in zip(quarters,quarters[1:])):
                metrics['ttm_revenue']=sum(x['revenue'] for x in quarters);metrics['ttm_net_income']=sum(x['net_income'] for x in quarters)
        disclosures=[]
        for x in self.context.get('disclosures',[]):
            if x['symbol']==sym and 0<=age(x['published_at'],t)<=30*86400:
                disclosures.append({**x,'disclosure_lag_days':(stamp(x['published_at']).date()-dt.date.fromisoformat(x['trade_date'])).days})
        blocked=next((x for x in events if x.get('kind') in ('halt','suspension','earnings_due') and x.get('reviewed') is True),'')
        adjustment=0
        if metrics.get('revenue_yoy_pct') is not None and metrics.get('net_margin_pct') is not None:
            # Small contextual adjustment only; it is not a directional forecast.
            if age(financials[-1]['published_at'])<120*86400:
                adjustment=2 if metrics['revenue_yoy_pct']>0 and metrics['net_margin_pct']>0 else -2
        return {'events':events[:10],'financials':metrics,'disclosures':disclosures[:10],
          'blocked':blocked.get('title','Event risk') if blocked else '', 'score_adjustment':adjustment,
          'coverage':'Available' if events or financials or disclosures else 'Unavailable'}
    def make_plan(self,s):
        reasons=[];t=now()
        for x in self.near_contracts(s):
            q=self.quote(x['instrument_token']);tick=finite(x.get('tick_size'),.05);lot=int(x['lot_size'])
            if not self.quote_ok(q):reasons.append('Waiting for fresh option depth');continue
            spread=(q['ask']-q['bid'])/max(q['ask'],.001)*100
            if spread>self.cfg.max_spread_pct:reasons.append('Option spread exceeds your limit');continue
            expiry=str(x['expiry'])[:10];years=max((stamp(expiry+'T15:30:00+05:30')-t).total_seconds()/31536000,1e-6)
            iv=implied_vol((q['bid']+q['ask'])/2,s['spot'],finite(x['strike']),years,s['direction'])
            if iv is None:reasons.append('Cannot fit IV to this quote');continue
            bump=max(.01,s['spot']*.0001)
            delta=float((bs(s['spot']+bump,finite(x['strike']),years,iv,s['direction'])-bs(s['spot']-bump,finite(x['strike']),years,iv,s['direction']))/(2*bump))
            if not .25<=abs(delta)<=.8:reasons.append('Contract delta outside 0.25–0.80');continue
            entry=rounded(q['ask']*(1+self.cfg.slippage_bps/10000),tick,True)
            distance=max(abs(delta)*s['risk_distance'],(q['ask']-q['bid'])*3,entry*.05)
            stop=rounded(entry-distance,tick);target=rounded(entry+self.cfg.reward_risk*(entry-stop),tick,True)
            if stop<=tick:reasons.append('Volatility stop consumes the premium; skip contract');continue
            plan={**s,'contract':x['tradingsymbol'],'exchange':x['exchange'],'token':int(x['instrument_token']),
               'lot_size':lot,'tick_size':tick,'strike':finite(x['strike']),'expiry':expiry,'iv':iv,'delta':delta,
               'entry':entry,'stop':stop,'target':target,'spread_pct':spread,'created':iso(t),'quote_time':q['at'],
               'cost_per_unit':(entry+stop)*self.cfg.fee_bps/10000+2*self.cfg.brokerage/lot}
            plan['plan_id']=digest([s['fingerprint'],plan['contract'],entry,stop,target])
            return plan,''
        return None,(reasons[0] if reasons else 'No eligible option contract in the current instrument master')
    def analysis_tick(self):
        t=now()
        for key,future in list(self.futures.items()):
            if future.done():
                try:self.replays[key]=future.result()
                except Exception as exc:self.replays[key]={'status':'ERROR','note':safe_error(exc),'plan_id':key}
                del self.futures[key]
        with self.lock:universe=dict(self.universe);bars=dict(self.bars);oldplans=dict(self.plans)
        candidates=[];plans={}
        for symbol,u in universe.items():
            s=signal_for(symbol,bars.get(symbol,[]),t,self.cfg,u['kind']=='INDEX')
            s['feed']=self.candle_status.get(symbol,{});s['context']=self.context_for(s)
            if s['status']=='SETUP':
                existing=oldplans.get(symbol)
                if existing and existing['fingerprint']==s['fingerprint'] and age(existing['created'])<90:
                    p=existing;reason=''
                else:p,reason=self.make_plan(s)
                if p:
                    plans[symbol]=p;replay=self.replays.get(p['plan_id'],{'status':'QUEUED','note':'Queued in background','plan_id':p['plan_id']})
                    if p['plan_id'] not in self.replays and p['plan_id'] not in self.futures and len(self.futures)<3 and self.pool:
                        self.futures[p['plan_id']]=self.pool.submit(replay_job,bars.get(symbol,[]),p,asdict(self.cfg))
                    simadj=0
                    if replay['status']=='READY':simadj=float(np.clip(replay['mean_r_after_estimated_costs']*10,-12,12))
                    s['score']=round(float(np.clip(s['score']+simadj+s['context']['score_adjustment'],0,100)),1)
                    s['plan']=p;s['simulation']=replay
                    checks,quantity,binding=self.risk_check(p,s['score'],s['context'],replay,self.mode)
                    s['checks']=checks;s['quantity']=quantity;s['binding']=binding
                    s['status']='READY' if not checks else 'WAIT';s['reason']=checks[0] if checks else 'Qualified; waiting for arm/entry worker'
                else:s['status']='WAIT';s['reason']=reason
            candidates.append(s)
        candidates.sort(key=lambda x:(x['status']=='READY',x.get('score',0)),reverse=True)
        with self.lock:self.candidates=candidates;self.plans=plans
        if len(self.replays)>600:self.replays={k:v for k,v in self.replays.items() if k in {p['plan_id'] for p in plans.values()}}
    def risk_check(self,p,score,context,replay,mode,exclude_id=None):
        cfg=self.cfg;t=now();reasons=[];q=self.quote(p['token'])
        if not session(t,cfg)['entry']:reasons.append('Outside the configured entry window')
        if self.master_date!=t.date().isoformat():reasons.append('Today’s instrument master is not ready')
        if age(p['trigger'],t)>cfg.max_candle_age or stamp(p['trigger']).date()!=t.date():reasons.append('Entry trigger expired')
        if not self.quote_ok(q):reasons.append('Option depth is stale or unavailable')
        elif (q['ask']-q['bid'])/max(q['ask'],.001)*100>cfg.max_spread_pct:reasons.append('Option spread widened')
        elif q['ask']>p['entry'] or abs(q['ask']-p['entry'])>(p['entry']-p['stop'])*.25:reasons.append('Quote moved outside the planned entry')
        underlying=self.universe.get(p['symbol'])
        uq=self.quote(underlying['token']) if underlying else {}
        if not self.quote_ok(uq,False):reasons.append('Underlying quote is stale')
        elif abs(uq['ltp']-p['spot'])>p['atr']*.75:reasons.append('Underlying moved too far from the trigger')
        if score<cfg.min_score:reasons.append('Setup score below configured minimum')
        if context.get('blocked'):reasons.append('Event risk: '+context['blocked'])
        if cfg.require_context and context.get('coverage')=='Unavailable' and p['kind']=='STOCK':reasons.append('News/results/disclosure coverage required')
        if cfg.require_simulation and replay.get('status')!='READY':reasons.append('Time Machine evidence required')
        if not 0<p['stop']<p['entry']<p['target']:reasons.append('Invalid stop/target geometry')
        if (p['target']-p['entry'])/max(p['entry']-p['stop'],.001)<cfg.reward_risk-.01:reasons.append('Reward/risk below minimum')
        positions=self.db.positions(mode);orders=[o for o in self.db.orders(active=True,mode=mode) if o['id']!=exclude_id]
        entries=[o for o in orders if o['kind']=='entry'];totals=self.db.totals(mode)
        used={x['id'] for x in positions}|{x['position_id'] for x in entries}
        heldplans=[x['plan'] for x in positions]+[x['plan'] for x in entries if x['position_id'] not in {a['id'] for a in positions}]
        if len(used)>=cfg.max_positions:reasons.append('Simultaneous position limit reached')
        if any(x['symbol']==p['symbol'] for x in heldplans):reasons.append('Underlying already owned or pending')
        if sum(x['direction']==p['direction'] for x in heldplans)>=cfg.max_same_direction:reasons.append('Same-direction position limit reached')
        sector=cfg.sectors.get(p['symbol'])
        if sector and any(cfg.sectors.get(x['symbol'])==sector for x in heldplans):reasons.append('Sector already represented')
        for hp in heldplans:
            a={x['ts']:x['close'] for x in self.bars.get(p['symbol'],[])[-61:]};b={x['ts']:x['close'] for x in self.bars.get(hp['symbol'],[])[-61:]}
            keys=sorted(a.keys()&b.keys())
            if len(keys)>=30:
                ra=np.diff(np.log([a[k] for k in keys]));rb=np.diff(np.log([b[k] for k in keys]))
                if np.std(ra)>1e-10 and np.std(rb)>1e-10 and abs(float(np.corrcoef(ra,rb)[0,1]))>=cfg.max_correlation:
                    reasons.append('Highly correlated with '+hp['symbol']);break
        if totals['trades']>=cfg.max_trades:reasons.append('Daily entry count reached')
        if self.db.get('daily_halt_'+mode)==t.date().isoformat():reasons.append('Daily loss halt is latched until the next session')
        if mode=='live':
            if os.getenv('ENABLE_LIVE_TRADING','').lower()!='true':reasons.append('Live execution is disabled on this server')
            if not self.broker.token:reasons.append('Kite login required')
            if age(self.account.get('checked'),t)>15:reasons.append('Broker account verification is stale')
            if self.account.get('external'):reasons.append('Unmanaged broker option exposure; reconcile in Kite')
            if any(o['status']=='UNKNOWN' for o in orders):reasons.append('Uncertain live order requires reconciliation')
        if any(not self.quote_ok(self.quote(pos['plan']['token'])) for pos in positions):
            reasons.append('An owned position has stale depth; new exposure waits')
        if mode=='live':
            for pos in positions:
                protected=any(o['position_id']==pos['id'] and o['kind']=='protect' and o['broker_id']
                    and o['status'] in ('OPEN','TRIGGER PENDING') and o['qty']-o['filled']==pos['qty'] for o in orders)
                if not protected:reasons.append('Broker protection pending for '+pos['plan']['contract'])
        exposure=sum(x['entry']*x['qty'] for x in positions)
        reserved=sum((x['qty']-x['filled'])*x['price'] for x in entries)
        cash=max(0,cfg.capital*(1-cfg.cash_buffer_pct/100)+totals['all_realised']-exposure-reserved)
        if mode=='live':cash=min(cash,max(0,finite(self.account.get('cash'))-reserved))
        risk=p['entry']-p['stop'];unit_cost=(p['entry']+p['stop'])*cfg.fee_bps/10000
        directional=sum(x['entry']*x['qty'] for x in positions if x['plan']['direction']==p['direction'])+sum((x['qty']-x['filled'])*x['price'] for x in entries if x['plan']['direction']==p['direction'])
        limits={'stop risk':max(0,cfg.risk_per_trade-2*cfg.brokerage)/max(risk+unit_cost,.001),
                'premium cap':cfg.max_premium/p['entry'],'cash buffer':max(0,cash-2*cfg.brokerage)/(p['entry']*(1+cfg.fee_bps/10000)),
                'direction exposure':max(0,cfg.direction_premium_cap-directional)/p['entry'],
                'visible ask depth':q.get('ask_qty',0)}
        binding=min(limits,key=limits.get);lot=p['lot_size'];qty=max(0,int(limits[binding]//lot)*lot)
        if qty==0:reasons.append('One lot does not fit '+binding)
        open_risk=sum(max(0,x['entry']-x['plan']['stop'])*x['qty'] for x in positions)
        open_risk+=sum(max(0,x['price']-x['plan']['stop'])*(x['qty']-x['filled']) for x in entries)
        if max(0,-totals['realised'])+open_risk+qty*(risk+unit_cost)+2*cfg.brokerage>cfg.daily_loss:reasons.append('Combined open risk exceeds daily loss allowance')
        if self.halt:reasons.append(self.halt)
        return list(dict.fromkeys(reasons)),qty,binding
    def supervise_tick(self):
        t=now();positions=self.db.positions()
        for mode in ['paper','live']:
            total=self.db.totals(mode)['realised'];usable=True
            for p in [x for x in positions if x['mode']==mode]:
                q=self.quote(p['plan']['token'])
                if self.quote_ok(q):total+=(q['bid']-p['entry'])*p['qty']
                else:usable=False
            if total<=-self.cfg.daily_loss:
                self.db.set('daily_halt_'+mode,t.date().isoformat())
                if self.mode==mode:self.armed=False
                self.db.event('error','risk',mode.upper()+' daily loss limit reached; exits continue',f'loss:{mode}:{t.date()}')
                for p in positions:
                    if p['mode']==mode:self.db.mark_exit(p['id'],'Daily loss limit')
        for p in positions:
            q=self.quote(p['plan']['token']);plan=p['plan'];reason=''
            # Clock-driven exits still request cancellation even when data is stale.
            if t.strftime('%H:%M')>=min(self.cfg.flat_by,session(t,self.cfg)['end']):reason='Flat-by time'
            elif self.quote_ok(q):
                if q['bid']<=plan['stop']:reason='Stop reached'
                elif q['bid']>=plan['target']:reason='Target reached'
            else:self.db.event('warning','feed',f'Fresh depth unavailable for owned {plan["contract"]}; broker stop remains active if acknowledged',f'stale:{p["id"]}:{int(time.time()//60)}')
            if reason:
                self.db.mark_exit(p['id'],reason);self.db.event('warning','exit',plan['contract']+' · '+reason,f'exit:{p["id"]}:{reason}')
        # Arming is per session and does not survive restart or date rollover.
        if self.armed and (self.arm_day!=t.date().isoformat() or not self.broker.token):self.armed=False
        if not self.armed:return
        with self.control:
            for s in list(self.candidates):
                if s['status']!='READY' or not s.get('plan'):continue
                p=s['plan'];checks,qty,_=self.risk_check(p,s['score'],s['context'],s['simulation'],self.mode)
                if checks:continue
                self.db.create_order(self.mode,'entry',p,'BUY',qty,p['entry'])
                # Next candidate is evaluated against the newly reserved order.
    def matches(self,o,b):
        return (b.get('tradingsymbol')==o['plan']['contract'] and b.get('exchange')==o['plan']['exchange']
          and b.get('transaction_type')==o['side'] and int(b.get('quantity',0))==o['qty'] and b.get('product')=='MIS')
    def reconcile_book(self,book):
        known=self.db.orders(active=True,mode='live')
        for o in known:
            if o['status']=='CREATED':continue
            matches=[b for b in book if (str(b.get('order_id'))==o['broker_id'] if o['broker_id'] else b.get('tag')==o['tag']) and self.matches(o,b)]
            if len(matches)!=1:
                if not o['broker_id'] and age(o['created'])>10:self.db.update_order(o['id'],status='UNKNOWN',note='No unique exact-tag broker match. Supply the order ID; no automatic resubmission.')
                continue
            b=matches[0];bid=str(b['order_id'])
            if o['broker_id']!=bid:self.db.update_order(o['id'],broker_id=bid)
            status=str(b.get('status','UNKNOWN')).upper()
            if status not in TERMINAL and status not in ('OPEN','TRIGGER PENDING'):status='OPEN'
            self.db.apply_report(o['id'],int(b.get('filled_quantity',0)),finite(b.get('average_price')),status,self.cfg)
    def account_tick(self,book):
        positions=self.broker.call('account','positions').get('net',[])
        margins=self.broker.call('account','margins','equity')
        expected=defaultdict(int)
        for p in self.db.positions('live'):expected[(p['plan']['exchange'],p['plan']['contract'],'MIS')]+=p['qty']
        actual={(p['exchange'],p['tradingsymbol'],p['product']):int(p.get('quantity',0)) for p in positions if int(p.get('quantity',0))}
        external=[];owned={o['broker_id'] for o in self.db.orders(active=True,mode='live') if o['broker_id']}
        option_contracts={(x['exchange'],x['tradingsymbol']) for values in self.options.values() for x in values}
        for key,value in actual.items():
            if key[:2] in option_contracts and value!=expected.get(key,0):external.append({'contract':key[1],'quantity':value,'local':expected.get(key,0),'reason':'Broker quantity differs'})
        for key,value in expected.items():
            if actual.get(key,0)!=value and not any(e['contract']==key[1] for e in external):external.append({'contract':key[1],'quantity':actual.get(key,0),'local':value,'reason':'Local position missing/different at broker'})
        for b in book:
            if b.get('status') not in TERMINAL and str(b.get('order_id')) not in owned and (b.get('exchange'),b.get('tradingsymbol')) in option_contracts:
                external.append({'contract':b['tradingsymbol'],'order_id':str(b['order_id']),'reason':'Unmanaged pending option order'})
        self.account={'checked':iso(),'cash':max(0,finite(margins.get('available',{}).get('live_balance',margins.get('net',0)))), 'external':external,'error':''}
        self.broker.status='Connected'
    def cancel_order(self,o):
        if o['status'] in TERMINAL:return
        if o['mode']=='paper':self.db.update_order(o['id'],status='CANCELLED',note='Cancelled remaining paper quantity');return
        if not o['broker_id']:return
        if o.get('cancel_at') and age(o['cancel_at'])<5:return
        self.db.update_order(o['id'],cancel_at=iso(),note='Cancellation requested; awaiting broker terminal status')
        try:self.broker.cancel(o)
        except Exception as e:self.db.update_order(o['id'],note='Cancel outcome awaiting reconciliation: '+safe_error(e))
    def paper_fill(self,o):
        q=self.quote(o['plan']['token'])
        if not self.quote_ok(q):return
        seq=q.get('at')+'|'+str(q.get('bid_qty'))+'|'+str(q.get('ask_qty'))+'|'+str(q.get('bid'))+'|'+str(q.get('ask'))
        if seq==o.get('quote_stamp'):return
        if o['kind']=='protect' and q['bid']>o['trigger']:return
        px=q['ask'] if o['side']=='BUY' else q['bid'];depth=q['ask_qty'] if o['side']=='BUY' else q['bid_qty']
        if (o['side']=='BUY' and px>o['price']) or (o['side']=='SELL' and px<o['price']):return
        extra=min(o['qty']-o['filled'],depth)
        if extra<=0:return
        total=o['filled']+extra;avg=(o['notional']+extra*px)/total
        self.db.apply_report(o['id'],total,avg,'COMPLETE' if total==o['qty'] else 'OPEN',self.cfg)
        self.db.update_order(o['id'],quote_stamp=seq)
    def process_jobs(self):
        for _ in range(10):
            try:job=self.jobs.get_nowait()
            except queue.Empty:return
            try:
                action=job['action']
                if action in ('flatten','pause'):
                    for p in self.db.positions(job['mode']):
                        if action=='flatten':self.db.mark_exit(p['id'],'User requested close')
                    for o in self.db.orders(active=True,mode=job['mode']):
                        if o['kind']=='entry':self.cancel_order(o)
                elif action=='cancel':
                    o=next((o for o in self.db.orders(active=True) if o['id']==job['id']),None)
                    if not o:raise ValueError('Pending order not found')
                    self.cancel_order(o)
                elif action=='attach':
                    o=next((o for o in self.db.orders(active=True,mode='live') if o['id']==job['id']),None)
                    if not o:raise ValueError('Unresolved live order not found')
                    reports=self.broker.call('account','order_history',job['broker_id'])
                    if not reports or not self.matches(o,reports[-1]) or reports[-1].get('tag')!=o['tag']:raise ValueError('Broker ID/unique tag does not match this exact order')
                    self.db.update_order(o['id'],broker_id=job['broker_id'])
                    self.reconcile_book([reports[-1]])
                self.db.event('info','action',action+' processed',job['job_id'])
            except Exception as e:self.db.event('error','action',safe_error(e),job['job_id'])
            finally:self.jobs.task_done()
    def reconciliation_tick(self):
        if self.broker.token:
            try:
                book=self.broker.call('account','orders');self.reconcile_book(book)
                if age(self.account.get('checked'))>5:self.account_tick(book)
            except Exception as e:
                self.account['error']=safe_error(e);self.armed=False
                self.db.event('warning','broker',safe_error(e),f'broker:{int(time.time()//60)}')
                self.broker.status='Login required' if not self.broker.token else 'Broker request failed; retrying'
                raise
    def broker_tick(self):
        self.process_jobs()
        for o in sorted(self.db.orders(active=True),key=lambda x:{'exit':0,'protect':1,'entry':2}.get(x['kind'],3)):
            if o['status']=='CREATED':
                if o['kind']=='entry':
                    candidate=next((x for x in self.candidates if x.get('plan',{}).get('plan_id')==o['plan']['plan_id']),None)
                    # Recheck time, mode, quote and session immediately before transmission.
                    if not self.armed or self.mode!=o['mode'] or not session(now(),self.cfg)['entry'] or not self.quote_ok(self.quote(o['plan']['token'])) or not candidate:
                        self.db.update_order(o['id'],status='CANCELLED',note='Entry authorization or data expired before transmission');continue
                    checks,qty,_=self.risk_check(o['plan'],candidate['score'],candidate['context'],candidate['simulation'],o['mode'],o['id'])
                    if checks or qty<o['qty']:
                        self.db.update_order(o['id'],status='CANCELLED',note='Final risk check: '+(checks[0] if checks else 'Available size reduced'));continue
                    if o['mode']=='live' and (age(self.account.get('checked'))>15 or self.account.get('external')):
                        self.db.update_order(o['id'],status='CANCELLED',note='Broker reconciliation required');continue
                if not self.db.claim(o['id']):continue
                if o['mode']=='paper':self.db.update_order(o['id'],status='OPEN')
                else:
                    try:
                        bid=self.broker.place(o);self.db.update_order(o['id'],broker_id=str(bid),status='OPEN')
                    except (InputException,OrderException) as exc:self.db.update_order(o['id'],status='REJECTED',note=safe_error(exc))
                    except Exception as exc:
                        self.db.update_order(o['id'],status='UNKNOWN',note='Submission outcome uncertain: '+safe_error(exc));self.armed=False
            elif o['mode']=='paper':
                if o['kind']=='entry' and age(o['created'])>20:self.cancel_order(o)
                elif o['kind']=='exit' and age(o['created'])>15:self.cancel_order(o)
                else:self.paper_fill(o)
            elif o['kind']=='entry' and age(o['created'])>20 and o['broker_id']:self.cancel_order(o)
            elif o['kind']=='exit' and age(o['created'])>15 and o['broker_id']:self.cancel_order(o)
        # Protect/exit logic is independent of entry arming, simulations and daily loss gates.
        pending=self.db.orders(active=True)
        recent=self.db.orders()
        for pos in self.db.positions():
            p=pos['plan'];sell=[o for o in pending if o['position_id']==pos['id'] and o['side']=='SELL']
            buys=[o for o in pending if o['position_id']==pos['id'] and o['side']=='BUY']
            mismatch=any(e.get('contract')==p['contract'] for e in self.account.get('external',[])) if pos['mode']=='live' else False
            if mismatch:
                self.db.event('error','ownership','Broker quantity differs for '+p['contract']+'; inspect Kite before another sell',f'ownership:{pos["id"]}:{int(time.time()//60)}');continue
            failed_protection=[o for o in recent if o['position_id']==pos['id'] and o['kind']=='protect' and o['status']=='REJECTED']
            if failed_protection and not pos['exit_reason']:
                self.db.mark_exit(pos['id'],'Protective stop rejected');pos['exit_reason']='Protective stop rejected'
                if self.mode=='live':self.armed=False
                self.db.event('error','protection','Protective stop rejected for '+p['contract']+'; closing owned quantity',f'protection:{pos["id"]}')
            if pos['exit_reason']:
                for o in buys:self.cancel_order(o)
                for o in sell:
                    if o['kind']=='protect':self.cancel_order(o)
                if sell or buys:continue # Wait for confirmed cancellation/final fills before selling again.
                recent_exits=[o for o in recent if o['position_id']==pos['id'] and o['kind']=='exit']
                if recent_exits and age(recent_exits[0]['updated'])<3:continue
                if sum(o['status']=='REJECTED' for o in recent_exits)>=3:
                    self.db.event('error','exit','Repeated broker exit rejection for '+p['contract']+'; close/check in Kite',f'exit-rejected:{pos["id"]}')
                    continue
                q=self.quote(p['token'])
                if not self.quote_ok(q):continue
                price=rounded(q['bid']*.995,p['tick_size'])
                if price>0:self.db.create_order(pos['mode'],'exit',p,'SELL',pos['qty'],price,pos['id'])
            elif not sell:
                # Synthetic exits in paper; acknowledged exchange SL limit in live.
                if pos['mode']=='live':
                    price=max(p['tick_size'],rounded(p['stop']*.985,p['tick_size']))
                    self.db.create_order('live','protect',p,'SELL',pos['qty'],price,pos['id'])
            elif sell[0]['kind']=='protect' and sell[0]['qty']-sell[0]['filled']!=pos['qty']:
                self.cancel_order(sell[0]) # Resize only after terminal cancellation.
    def context_tick(self):
        url='https://nsearchives.nseindia.com/content/RSS/Online_announcements.xml'
        try:
            response=requests.get(url,timeout=(3,6),headers={'User-Agent':'OptionDeskIndia/1.0 (personal research)'})
            response.raise_for_status()
            if len(response.content)>5_000_000:raise ValueError('Announcement feed too large')
            root=ET.fromstring(response.content);events=[]
            from email.utils import parsedate_to_datetime
            for item in root.findall('.//item')[:150]:
                title=item.findtext('title','');description=item.findtext('description','');link=item.findtext('link','')
                published=item.findtext('pubDate','')
                try:published=iso(parsedate_to_datetime(published))
                except (ValueError,TypeError):continue
                matches=[s for s in self.universe if re.search(r'(?<![A-Z0-9])'+re.escape(s)+r'(?![A-Z0-9])',(title+' '+description).upper())]
                for sym in matches:
                    kind='results' if re.search('result|earnings',title,re.I) else 'disclosure' if re.search('insider|promoter|acquisition',title,re.I) else 'announcement'
                    events.append({'symbol':sym,'title':re.sub('<[^>]+>',' ',title)[:240],'published_at':published,'source_url':link,'kind':kind,'reviewed':False})
            self.feed_events=events;self.context_health={'status':'NSE RSS available','updated':iso(),'matched_events':len(events)}
        except Exception as e:
            self.context_health={'status':'Unavailable: '+safe_error(e),'updated':iso(),'matched_events':len(self.feed_events)}
    def import_context(self,data):
        if set(data)-{'events','financials','disclosures'}:raise ValueError('Use events, financials and disclosures arrays')
        output={}
        for category in ['events','financials','disclosures']:
            rows=data.get(category,[])
            if not isinstance(rows,list) or len(rows)>3000:raise ValueError('Invalid context size')
            unique={}
            for row in rows:
                if not isinstance(row,dict):raise ValueError('Context rows must be objects')
                for k in ['symbol','source_url','published_at']:
                    if not isinstance(row.get(k),str) or not row[k]:raise ValueError('Every row needs '+k)
                parsed=urlparse(row['source_url'])
                if parsed.scheme!='https' or not parsed.hostname:raise ValueError('Source must be an HTTPS source document URL')
                if age(row['published_at'])<0:raise ValueError('Publication time cannot be in the future')
                if category=='financials':
                    dt.date.fromisoformat(row['period'])
                    for key in ['revenue','net_income']:
                        if not isinstance(row.get(key),(int,float)) or not math.isfinite(row[key]):raise ValueError('Invalid reported '+key)
                    if row.get('period_type') not in ('quarter','annual') or not row.get('basis'):raise ValueError('Financials need period_type and basis (consolidated/standalone)')
                    if row['revenue']<0:raise ValueError('Negative revenue is not supported')
                    key=(row['symbol'],row['period'],row['basis'],row['period_type'])
                elif category=='disclosures':
                    day=dt.date.fromisoformat(row['trade_date'])
                    if day>stamp(row['published_at']).date():raise ValueError('Trade date cannot follow disclosure date')
                    if row.get('side') not in ('BUY','SELL','OTHER') or not row.get('owner'):raise ValueError('Disclosure needs owner and BUY/SELL/OTHER')
                    key=digest(row)
                else:
                    if not row.get('title') or row.get('kind') not in ('announcement','results','halt','suspension','earnings_due','macro'):raise ValueError('Invalid event kind/title')
                    if 'reviewed' in row and not isinstance(row['reviewed'],bool):raise ValueError('reviewed must be a boolean')
                    key=digest(row)
                unique[str(key)]=row
            output[category]=list(unique.values())
        with self.control:self.context=output;self.db.set('context',output)
        self.db.event('info','context','Source-backed news, results and disclosure context imported')
    def publish(self):
        positions=self.db.positions();orders=self.db.orders();active=self.db.orders(active=True)
        for p in positions:
            q=self.quote(p['plan']['token']);fresh=self.quote_ok(q)
            p['mark']=q.get('bid') if fresh else None
            p['unrealised']=(q['bid']-p['entry'])*p['qty'] if fresh else None
        tally={}
        for mode in ['paper','live']:
            totals=self.db.totals(mode);owned=[p for p in positions if p['mode']==mode]
            totals['unrealised']=sum(p['unrealised'] or 0 for p in owned)
            totals['marks_complete']=all(p['unrealised'] is not None for p in owned)
            totals['exposure']=sum(p['qty']*p['entry'] for p in owned);totals['positions']=len(owned)
            tally[mode]=totals
        with self.lock:
            candidates=copy.deepcopy(self.candidates);health=copy.deepcopy(self.health)
            for name,item in health.items():
                item['age_seconds']=round(age(item['at']),1)
                if item['age_seconds']>max(20,360 if name=='context' else 3600 if name=='catalog' else 20):item['status']='delayed'
            fresh=sum(bool(b) and age(b[-1]['ts'])<=self.cfg.max_candle_age for b in self.bars.values())
        snap={'build':BUILD,'ready':True,'server_time':iso(),'mode':self.mode,'armed':self.armed,
          'market':session(now(),self.cfg),'broker':{'status':self.broker.status,'configured':bool(self.broker.key and self.broker.secret),
          'user':self.broker.user,'feed':self.broker.feed,'authenticated':bool(self.broker.token)},'account':self.account,
          'live_enabled':os.getenv('ENABLE_LIVE_TRADING','').lower()=='true','config':asdict(self.cfg),'health':health,
          'universe_count':len(self.universe),'fresh_count':fresh,'catalog_date':self.master_date,'candidates':candidates,
          'positions':positions,'orders':orders[:60],'pending_orders':active,'totals':tally,'events':self.db.events(),
          'context_health':self.context_health,'skills':[{'id':x[0],'name':x[1],'detail':x[2]} for x in SKILLS],
          'halt':self.halt,'calendar_note':'Weekday schedule plus configured holidays/special sessions; fresh exchange data is required for entry.'}
        with self.lock:self.snapshot=snap
    def state(self):
        # Pure memory snapshot: no requests, DB calls, history downloads or fitting.
        with self.lock:return copy.deepcopy(self.snapshot)
    def command(self,data):
        action=data.get('action');mode=data.get('mode',self.mode)
        if mode not in ('paper','live'):raise ValueError('Invalid mode')
        with self.control:
            if action=='disarm':
                self.armed=False;self.jobs.put_nowait({'action':'pause','mode':self.mode,'job_id':uuid.uuid4().hex});self.db.event('info','control','Entries paused; pending entries will be cancelled; position supervision continues')
            elif action=='arm':
                if not self.broker.token:raise ValueError('Sign in to Kite for market data first')
                if not session(now(),self.cfg)['entry']:raise ValueError('Arming is available during the entry window')
                if mode=='live':
                    if os.getenv('ENABLE_LIVE_TRADING','').lower()!='true':raise ValueError('Server live trading is disabled')
                    if data.get('confirmation')!='ARM LIVE':raise ValueError('Type ARM LIVE to authorize this session')
                    if age(self.account.get('checked'))>15 or self.account.get('external'):raise ValueError('Fresh, reconciled broker account required')
                    if any(o['status']=='UNKNOWN' for o in self.db.orders(active=True,mode='live')):raise ValueError('Resolve uncertain live order first')
                if self.db.get('daily_halt_'+mode)==now().date().isoformat():raise ValueError('Daily loss halt remains active')
                self.mode=mode;self.arm_day=now().date().isoformat();self.armed=True
                self.db.event('warning' if mode=='live' else 'info','control',mode.upper()+' entries armed for this session')
            elif action=='mode':
                if self.armed:raise ValueError('Pause entries before changing mode')
                self.mode=mode
            elif action=='refresh':self.refresh.set()
            elif action in ('flatten','cancel','attach'):
                if action in ('cancel','attach'):
                    order=next((o for o in self.db.orders(active=True) if o['id']==data.get('id')),None)
                    if not order:raise ValueError('Pending order not found')
                    if order['mode']!=mode or (action=='attach' and mode!='live'):raise ValueError('Action mode does not match the order')
                    if action=='cancel' and order['kind']=='protect':
                        raise ValueError('Use Close positions to cancel protection and exit safely')
                if action=='flatten':self.armed=False
                if mode=='live' and data.get('confirmation')!='CONFIRM':raise ValueError('Confirm the live account action')
                job={**data,'mode':mode,'job_id':uuid.uuid4().hex};self.jobs.put_nowait(job)
                return {'ok':True,'queued':True,'job_id':job['job_id']}
            else:raise ValueError('Unknown action')
        return {'ok':True}

# ---- HTTP surface: small state reads and short validated control requests ----
SESSIONS={};SESSION_LOCK=threading.Lock()
PUBLIC_ORIGIN=os.getenv('PUBLIC_ORIGIN','https://algo.wecon.in').rstrip('/')
ENGINE=None;PROCESS_LOCK=None

def get_session(request,required=True):
    token=request.cookies.get('od_session','')
    with SESSION_LOCK:
        s=SESSIONS.get(token)
        if s and s['expires']>time.time():return s
    if required:raise HTTPException(401,'Workspace session expired; reconnecting')
    return None

def same_origin(request):
    origin=request.headers.get('origin')
    expected=PUBLIC_ORIGIN or str(request.base_url).rstrip('/')
    if request.headers.get('sec-fetch-site')=='cross-site' or (origin and origin!=expected):
        raise HTTPException(403,'Origin does not match this workspace')

def guard(request,write=False):
    s=get_session(request)
    if write:
        if not hmac.compare_digest(request.headers.get('x-csrf-token',''),s['csrf']):raise HTTPException(403,'Invalid request token; reload the page')
        same_origin(request)
    return s

@contextlib.asynccontextmanager
async def lifespan(app):
    global ENGINE,PROCESS_LOCK
    folder=Path(os.getenv('OPTION_DESK_DATA',str(ROOT/'option_desk_data')));folder.mkdir(parents=True,exist_ok=True)
    PROCESS_LOCK=open(folder/'engine.lock','a+')
    try:
        if os.name=='nt':
            import msvcrt
            PROCESS_LOCK.seek(0);PROCESS_LOCK.write('1');PROCESS_LOCK.flush();PROCESS_LOCK.seek(0)
            msvcrt.locking(PROCESS_LOCK.fileno(),msvcrt.LK_NBLCK,1)
        else:
            import fcntl
            fcntl.flock(PROCESS_LOCK,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except OSError:raise RuntimeError('Another Option Desk process is running against this database. Use one worker.')
    ENGINE=Engine(folder);ENGINE.publish();ENGINE.start()
    yield
    ENGINE.close();PROCESS_LOCK.close()

app=FastAPI(title='Option Desk India',docs_url=None,redoc_url=None,openapi_url=None,lifespan=lifespan)

@app.middleware('http')
async def headers(request,call_next):
    if request.headers.get('content-length','').isdigit() and int(request.headers['content-length'])>2_000_000:
        return JSONResponse({'detail':'Request is too large'},status_code=413)
    response=await call_next(request)
    response.headers.update({'Cache-Control':'no-store','X-Content-Type-Options':'nosniff',
      'Referrer-Policy':'no-referrer','X-Frame-Options':'DENY',
      'Content-Security-Policy':"default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"})
    return response

@app.exception_handler(ValueError)
async def value_error(request,exc):return JSONResponse({'detail':str(exc)[:250]},status_code=400)
@app.exception_handler(queue.Full)
async def queue_error(request,exc):return JSONResponse({'detail':'Control queue full; retry shortly'},status_code=429)

@app.get('/')
async def home():return FileResponse(ROOT/'index.html',media_type='text/html')
@app.get('/healthz')
async def healthz():return {'ok':ENGINE is not None,'build':BUILD,'time':iso()}
@app.post('/api/session')
async def open_workspace(request:Request):
    # Passwordless by explicit owner request. This session provides CSRF and
    # Kite callback binding, not access control. APP_PASSWORD is no longer used.
    same_origin(request)
    existing=get_session(request,False)
    if existing:return {'ok':True,'csrf':existing['csrf']}
    sid=secrets.token_urlsafe(32);csrf=secrets.token_urlsafe(24)
    with SESSION_LOCK:
        for key in list(SESSIONS):
            if SESSIONS[key]['expires']<time.time():del SESSIONS[key]
        SESSIONS[sid]={'csrf':csrf,'expires':time.time()+12*3600,'login_pending':0}
    response=JSONResponse({'ok':True,'csrf':csrf})
    response.set_cookie('od_session',sid,httponly=True,samesite='lax',secure=PUBLIC_ORIGIN.startswith('https://') or request.url.scheme=='https',max_age=12*3600)
    return response
@app.get('/api/session')
async def session_info(request:Request):
    s=get_session(request,False)
    return {'authenticated':bool(s),'csrf':s['csrf'] if s else None,'password_required':False}
@app.get('/api/state')
async def state(request:Request):guard(request);return ENGINE.state()
@app.post('/api/control')
async def control(request:Request):
    guard(request,True);data=await request.json();return ENGINE.command(data)
@app.post('/api/settings')
async def settings(request:Request):
    guard(request,True);data=await request.json();cfg=Config.parse(data)
    with ENGINE.control:
        if ENGINE.armed:raise ValueError('Pause entries before editing settings')
        if ENGINE.db.positions() or ENGINE.db.orders(active=True):raise ValueError('Keep risk settings stable while positions/orders are active')
        ENGINE.cfg=cfg;ENGINE.db.set('config',asdict(cfg));ENGINE.master_date=''
    return {'ok':True}
@app.post('/api/context')
async def evidence(request:Request):guard(request,True);ENGINE.import_context(await request.json());return {'ok':True}
@app.post('/api/kite/login')
async def kite_login(request:Request):
    s=guard(request,True);url=ENGINE.broker.login_url();s['login_pending']=time.time()+300
    return {'url':url}
@app.get('/api/callback')
def kite_callback(request:Request,request_token:str='',status:str=''):
    s=get_session(request)
    if s.get('login_pending',0)<time.time():raise HTTPException(403,'Start Kite login from this workspace')
    s['login_pending']=0
    if status!='success' or not request_token:raise HTTPException(400,'Kite login was not completed')
    try:ENGINE.broker.login(request_token)
    except Exception as e:raise HTTPException(400,safe_error(e))
    ENGINE.db.event('info','login','Kite connected; entries remain under your control')
    return RedirectResponse('/',status_code=303)
@app.get('/api/diagnostics')
async def diagnostics(request:Request):
    guard(request);s=ENGINE.state()
    # No credentials, request tokens, session cookies or raw access tokens.
    return JSONResponse(s,headers={'Content-Disposition':'attachment; filename="option-desk-diagnostics.json"'})

if __name__=='__main__':
    import uvicorn
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
    uvicorn.run(app,host=os.getenv('HOST','127.0.0.1'),port=int(os.getenv('PORT','5000')),workers=1,
                access_log=False,proxy_headers=True,forwarded_allow_ips=os.getenv('TRUSTED_PROXY_IPS','127.0.0.1'))

# Upstream workflow attribution / license:
# MIT License
# 
# Copyright (c) 2026 Jake Schincariol
# 
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
# 
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
# 
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
# 
