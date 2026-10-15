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
        assert client.get('/api/stock-ai/state').status_code==401
        app.SESSION['access_token']='offline-token'
        start=time.monotonic();r=client.get('/api/stock-ai/state');assert r.status_code==200,r.get_json()
        assert r.get_json()['dashboard']['status']=='LOADING'
        assert time.monotonic()-start<.25
        for _ in range(60):
            if not app._STOCK_READ_SNAPSHOT.running:break
            time.sleep(.02)
        r=client.get('/api/stock-ai/state');d=r.get_json();assert r.status_code==200 and not d['dashboard']['stale'],d
        json.dumps(d,allow_nan=False)
        assert d['runtime_build']==app.STOCK_RUNTIME_BUILD
        # Exercise the real Stock risk manager/SQLite schema without broker I/O.
        app.STOCK_DESK.risk_display=dict(config=app.STOCK_RISK.config(),states={m:app.STOCK_RISK.snapshot(m) for m in ('live','paper')},timestamp=app.now_ist().isoformat(),broker=app.BROKER_GATE.status())
        assert client.get('/api/desk-risk/stock').status_code==200
        r=client.post('/api/stock-ai/control/disarm',json={});assert r.status_code==200 and r.get_json()['config']['armed'] is False
        assert client.get('/api/stock-ai/state').get_json()['dashboard']['stale']
        assert client.get('/api/system-health').status_code==200
        assert not calls,calls
        # UI fixtures use real backend response shapes, no account data.
        Path(__file__).with_name('smoke_state.json').write_text(json.dumps(d))
        print('Auth, cold/warm state, JSON, independent risk, disarm, invalidation and health: PASS; external requests:',len(calls))
    finally:os.chdir(old)
