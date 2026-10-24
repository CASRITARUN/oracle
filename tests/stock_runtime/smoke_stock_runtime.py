"""Offline full-module/Flask smoke test in a disposable directory."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import tempfile
import time
os.environ.update(AI_START_WORKERS='0',WEBSITE_PASSWORD='offline-test-only',WEBSITE_SECRET_KEY='offline-test-secret',KITE_API_KEY='offline-test-key',KITE_API_SECRET='offline-test-secret')
import requests
import kiteconnect
calls=[]
def deny(*args,**kwargs):
    calls.append('BLOCKED');raise AssertionError('External network is disabled during smoke testing')
requests.sessions.Session.request=deny
kiteconnect.KiteConnect._request=deny
with tempfile.TemporaryDirectory() as path:
    source=Path(__file__).with_name('backend.py')
    if not source.exists():source=Path(__file__).resolve().parents[2]/'backend.py'
    target=Path(path)/'backend.py';shutil.copyfile(source,target)
    shutil.copyfile(source.with_name('index.html'),Path(path)/'index.html')
    old=os.getcwd();os.chdir(path)
    try:
        started=time.monotonic();spec=importlib.util.spec_from_file_location('stock_smoke_backend',target);app=importlib.util.module_from_spec(spec);spec.loader.exec_module(app)
        print('Full module initialized, seconds:',round(time.monotonic()-started,3))
        client=app.app.test_client()
        assert client.get('/api/stock-ai/state').status_code==401
        with client.session_transaction() as session:
            session['website_authenticated']=True;session['website_generation']=app._site_generation()
        # An old authenticated cookie without CSRF heals on the status endpoint.
        auth=client.get('/api/website-status');assert auth.status_code==200 and auth.get_json()['csrf']
        # The saved ledger remains readable without a Kite session, with an honest
        # disconnected flag. Website authentication is still required above.
        assert client.get('/api/stock-ai/state').status_code==200
        assert app._STOCK_READ_SNAPSHOT.done.wait(2)
        assert client.get('/api/stock-ai/state').get_json()['connected'] is False
        app.SESSION['access_token']='offline-token'
        app._STOCK_READ_SNAPSHOT.invalidate()
        start=time.monotonic();r=client.get('/api/stock-ai/state');assert r.status_code==200,r.get_json()
        assert r.get_json()['dashboard']['status'] in ('LOADING','CURRENT','STALE')  # pre-login snapshot may be retained while invalidation refreshes
        assert time.monotonic()-start<.25
        for _ in range(60):
            if not app._STOCK_READ_SNAPSHOT.running:break
            time.sleep(.02)
        r=client.get('/api/stock-ai/state');d=r.get_json();assert r.status_code==200 and not d['dashboard']['stale'],d
        json.dumps(d,allow_nan=False)
        assert d['runtime_build']==app.STOCK_RUNTIME_BUILD
        # Real production schemas with old/incomplete analytics and typed runtime
        # values. None of these display records may prevent the ledger from loading.
        import numpy as np
        with app.STOCK_DESK.db() as c:
            c.execute("INSERT INTO desk_positions(id,ts,symbol,mode,contract,exchange,lot,qty,entry,stop,target,risk,status,exit_ts,pnl,fees,setup) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ('offline-old','2025-01-02T09:30:00+05:30','TEST','paper','TESTCE','NFO',50,0,100.,90.,120.,1000.,'CLOSED',None,20.,None,'{}'))
            c.execute("INSERT INTO desk_positions(id,ts,symbol,mode,contract,exchange,lot,qty,entry,stop,target,risk,status,pnl,fees,setup) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ('offline-open',app.now_ist().isoformat(),'TEST','paper','TESTCE','NFO',50,100,100.,90.,120.,1000.,'OPEN',0.,5.,'{}'))
            c.execute("INSERT INTO desk_orders(id,ts,position_id,side,qty,mode,tag,broker_id,status,filled,notional,limit_price,reason) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ('offline-fill','2025-01-02T09:30:00+05:30','offline-old','BUY',50,'live','OFFLINE',None,'COMPLETE',50,5000.,100.,'Offline fixture'))
        app.STOCK_DESK.put('execution_fill:offline-fill',dict(mode='live',filled=50,delay_seconds=None))
        app.STOCK_DESK.health['offline-open']=dict(bid=101.,observed_mono=time.monotonic())
        app.STOCK_DESK.signals={'TEST':dict(symbol='TEST',decision='WAIT',confidence=np.float64(.55),rank_score=np.int64(55),qualified=np.bool_(False),bar_ts=app.now_ist(),reason='Offline fixture')}
        app._STOCK_READ_SNAPSHOT.invalidate()
        client.get('/api/stock-ai/state')
        assert app._STOCK_READ_SNAPSHOT.done.wait(2)
        d=client.get('/api/stock-ai/state').get_json()
        assert d['dashboard']['status']=='PARTIAL' and not d['dashboard']['stale'],d['dashboard']
        assert d['positions'][0]['qty']==100 and d['evidence']['net'] is None
        assert d['signals'][0]['rank_score']==55 and d['signals'][0]['qualified'] is False
        json.dumps(d,allow_nan=False)
        research,sizing=app.STOCK_DESK._review_trade_evidence(app.STOCK_DESK.rows("SELECT * FROM desk_positions WHERE status='CLOSED'"))
        assert research['modes']['paper']['mean_giveback'] is None
        assert all(key in d for key in ('execution_traces','option_model_review','option_rollback_available','evidence_recorder'))
        # Exercise the real Stock risk manager/SQLite schema without broker I/O.
        app.STOCK_DESK.risk_display=dict(config=app.STOCK_RISK.config(),states={m:app.STOCK_RISK.snapshot(m) for m in ('live','paper')},timestamp=app.now_ist().isoformat(),broker=app.BROKER_GATE.status())
        assert client.get('/api/desk-risk/stock').status_code==200
        r=client.post('/api/stock-ai/control/disarm',json={});assert r.status_code==200 and r.get_json()['config']['armed'] is False
        refreshed=client.get('/api/stock-ai/state').get_json()
        assert refreshed['dashboard']['stale'] or refreshed['config']['armed'] is False
        assert client.get('/api/system-health').status_code==200
        # Active Index uses its independent ledger and the new trade-local policy.
        app.INDEX_OPTIONS_DESK.a.market_open=lambda:False
        index_state=app.INDEX_OPTIONS_DESK.state()
        assert index_state['runtime_build']=='2026.10.24-index-independent.1'
        assert index_state['sizing_policy']['basis']=='trade-independent-v2'
        assert index_state['database']['status']=='OK'
        assert index_state['config']['armed'] is False and index_state['config']['mode']=='paper'
        assert app.INDEX_OPTIONS_DESK.shared_risk is app.INDEX_RISK
        assert app.INDEX_RISK is not app.STOCK_RISK
        json.dumps(index_state,allow_nan=False)
        Path(__file__).with_name('index_smoke_state.json').write_text(json.dumps(index_state))
        csrf=auth.get_json()['csrf'];generation=app._site_generation()
        lock=client.post('/website-logout',headers={'X-Website-CSRF':csrf})
        assert lock.status_code==200 and lock.get_json()['locked']
        assert app._site_generation()!=generation
        assert client.get('/api/stock-ai/state').status_code==401
        assert not calls,calls
        # UI fixtures use real backend response shapes, no account data.
        Path(__file__).with_name('smoke_state.json').write_text(json.dumps(d))
        print('Auth, cold/warm/populated state, typed JSON, legacy records, independent risk, disarm, invalidation and health: PASS; external requests:',len(calls))
    finally:
        if 'app' in locals():app._STOCK_READ_SNAPSHOT.done.wait(2)
        os.chdir(old)
