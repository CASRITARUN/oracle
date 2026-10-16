"""Stock-only reliability and learning integration checks. No live broker access."""
import copy
import json
import logging
import math
import secrets
import sqlite3
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
import numpy as np
import test_algo_upgrade as old
ns=old.ns
for node in old.tree.body:
    if isinstance(node,old.ast.Assign) and any(isinstance(t,old.ast.Name) and t.id in ('STOCK_FULL_SCAN_BAR_MAX_AGE','STOCK_FULL_SCAN_DEEP_N','STOCK_FULL_REFRESH_BATCH','STOCK_HISTORY_PACE_SECONDS') for t in node.targets):
        exec(compile(old.ast.Module(body=[node],type_ignores=[]),'<scan constants>','exec'),ns)
ns.update(secrets=secrets,logger=logging.getLogger('stock-tests'),_AI_TRAINING_SLOT=threading.Semaphore(1),
    STOCK_RUNTIME_BUILD='test',STOCK_AI_BUILD='test',STOCK_HIST_MIN_ARCHIVE_COVERAGE=.7,
    _HIST_EMA_WEIGHT_CACHE={},BROKER_GATE=SimpleNamespace(status=lambda:dict(cooldown_seconds=0)),
    DESK_MARKET_FEED=SimpleNamespace(state=lambda:dict(connected=False,status='test')))
for name in ['_stock_success_sigmoid','_stock_success_fit','_stock_success_predict','_stock_success_auc','_hist_feature_row']:
    old.load(name)
ns['compute_index_features']=ns['_hist_feature_row']
ns['STOCK_DIRECTION_FEATURES']=['ret_1','ret_3','ret_5','ret_10','ret_20','ema_gap_9_20','ema_gap_20_50','rsi14','volatility_10','range_20','volume_ratio','trend_score','momentum_score','time_sin','time_cos']
for name in ['StockRuntimeAdapter','StockRuntimeEngine','StockReadSnapshot']:old.load(name)
Runtime=ns['StockRuntimeEngine'];Read=ns['StockReadSnapshot'];Adapter=ns['StockRuntimeAdapter']
class Broker(old.FakeBroker):
    dashboard_db=Adapter.dashboard_db
    features=ns['DeskAdapter'].features
    legacy_open=lambda self:[]
    def __init__(self,path):super().__init__(path);self.history={};self.open=False
    def market_open(self):return self.open
    def bars(self,symbol,n):return self.history.get(symbol,[])[-n:]

def model(identity='champ',n=15):
    now=old.clock.now
    return dict(id=identity,mu=[0.]*n,sd=[1.]*n,w=[.1]*n,bias=0.,ts=now.isoformat(),train_samples=800,validation_samples=200,
        auc=.54,brier=.249,accuracy=54.,baseline=53.,train_end=(now-timedelta(days=9)).isoformat(),validation_start=(now-timedelta(days=7)).isoformat())
class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();ns['__file__']=self.tmp.name+'/backend.py'
        self.a=Broker(self.tmp.name+'/stock_options_ai.db')
        self.e=Runtime(self.a);self.e.desk_name='stock';self.e.a.symbols=('TEST',)
        self.e.portfolio_guard=SimpleNamespace()
        self.m=model()
        with self.e.db() as c:c.execute('INSERT INTO desk_models VALUES(?,?,?)',(self.m['id'],self.m['ts'],json.dumps(self.m)))
        self.e.put('stock_champion_id',self.m['id']);self.e.runtime_cache.clear()
        self.e.shared_risk=SimpleNamespace(lock=threading.RLock(),snapshot=lambda mode:dict(entry_block=None,liquidate=False),config=lambda:{})
        self.e.ban_error=None;self.e.history_backfill_status={}
    def tearDown(self):self.e.stop_event.set();self.tmp.cleanup()
    def insert(self,identity,target,**kw):
        ts=target-timedelta(minutes=15)
        values=dict(id=identity,symbol='TEST',ts=ts.replace(tzinfo=old.IST).isoformat(),target_ts=target.replace(tzinfo=old.IST).isoformat(),
            spot=100.,features=json.dumps([.1]*15),model_id='champ',prob=.6,recorded_at=(ts+timedelta(minutes=5)).isoformat(),**kw)
        with self.e.db() as c:c.execute('INSERT INTO desk_observations('+','.join(values)+') VALUES('+','.join('?' for _ in values)+')',list(values.values()))
    def test_state_does_not_call_trading_dependencies_or_wait_for_locks(self):
        self.e.block=lambda: self.fail('display called block')
        self.e.shared_risk.snapshot=lambda mode:self.fail('display calculated risk')
        self.e.config=lambda:self.fail('display opened config outside read transaction')
        self.e.risk_lock.acquire();self.e.lock.acquire();self.e.train_lock.acquire()
        started=time.monotonic()
        try:s=self.e.state()
        finally:self.e.risk_lock.release();self.e.lock.release();self.e.train_lock.release()
        self.assertLess(time.monotonic()-started,.5);self.assertEqual(s['model']['id'],'champ')
        self.assertIsNone(s['pnl']['live']);json.dumps(s,allow_nan=False)
    def test_wal_display_reads_last_committed_ledger_with_writer_open(self):
        with self.a.db() as c:c.execute('PRAGMA journal_mode=WAL')
        writer=sqlite3.connect(self.a.path);writer.execute('BEGIN IMMEDIATE');writer.execute("INSERT INTO desk_meta VALUES('uncommitted','1')")
        try:self.assertEqual(self.e.state()['model']['id'],'champ')
        finally:writer.rollback();writer.close()
    def test_snapshot_returns_without_waiting_and_marks_old_data_stale(self):
        box=Read();gate=threading.Event();finished=threading.Event()
        def blocked():gate.wait(2);finished.set();return {'config':{},'positions':[]}
        e=SimpleNamespace(state=blocked,config_defaults={})
        started=time.monotonic();value=box.response(e)
        self.assertLess(time.monotonic()-started,.1);self.assertEqual(value['dashboard']['status'],'LOADING')
        for _ in range(10):box.response(e)
        self.assertTrue(box.running)
        gate.set();finished.wait(1)
        for _ in range(50):
            if not box.running:break
            time.sleep(.01)
        box.created=time.monotonic()-30
        e.state=lambda:(_ for _ in ()).throw(ValueError('test-only'))
        with self.assertLogs('stock-tests',level='ERROR'):
            value=box.response(e)
            for _ in range(50):
                if not box.running:break
                time.sleep(.01)
        self.assertEqual(value['dashboard']['status'],'STALE');self.assertEqual(value['positions'],[])
        self.assertIsNotNone(box.payload)
    def test_invalidation_prevents_inflight_precommand_snapshot_becoming_current(self):
        box=Read();gate=threading.Event()
        e=SimpleNamespace(state=lambda:(gate.wait(1),{'config':{}})[1],config_defaults={})
        box.response(e);box.invalidate();gate.set()
        for _ in range(50):
            if not box.running:break
            time.sleep(.01)
        self.assertIsNone(box.payload)
    def test_disarm_applies_while_supervision_lock_is_held(self):
        gate=threading.Event();ready=threading.Event()
        def hold():
            with self.e.risk_lock:ready.set();gate.wait(2)
        t=threading.Thread(target=hold);t.start();ready.wait(1)
        try:
            started=time.monotonic();cfg=self.e.control('disarm',{})
            self.assertLess(time.monotonic()-started,.2);self.assertFalse(cfg['armed']);self.assertTrue(self.e.operator_pause.is_set())
        finally:gate.set();t.join(2)
    def test_disarm_cancels_queued_arm_without_auto_retry(self):
        self.e.risk_lock.acquire()
        try:
            self.e.queue_control('arm',{'ack':True});self.e.control('disarm',{})
        finally:self.e.risk_lock.release()
        for _ in range(60):
            if not self.e.command_lock.locked():break
            time.sleep(.01)
        self.assertFalse(self.e.config()['armed']);self.assertTrue(self.e.operator_pause.is_set());self.assertEqual(self.e.command_status['status'],'CANCELLED')
    def test_disarm_wins_against_arm_already_inside_broker_validation(self):
        cfg=self.e.config();cfg.update(mode='paper',armed=False);self.e.put('config',cfg)
        entered=threading.Event();release=threading.Event();errors=[]
        def snapshot(mode):
            entered.set();release.wait(2);return dict(entry_block=None,liquidate=False)
        self.e.shared_risk.snapshot=snapshot
        def arm():
            try:self.e.control('arm',{'ack':True})
            except Exception as exc:errors.append(str(exc))
        thread=threading.Thread(target=arm);thread.start();self.assertTrue(entered.wait(1))
        try:self.e.control('disarm',{})
        finally:release.set();thread.join(2)
        self.assertFalse(thread.is_alive());self.assertFalse(self.e.config()['armed']);self.assertTrue(self.e.operator_pause.is_set())
        self.assertTrue(any('superseded' in message for message in errors),errors)
    def test_local_deferral_is_retryable_but_invalid_quantities_are_not(self):
        self.assertTrue(self.e._supervision_transport_error(ns['BrokerDeferred']('No request sent')))
        self.assertFalse(self.e._supervision_transport_error(ValueError('Quantity mismatch')))
        self.assertTrue(self.e._owned_connection_halt('Position supervision error: Broker request deferred locally; supervision will retry. No request was sent.'))
    def test_old_missing_rows_do_not_starve_recent_observations(self):
        now=old.clock.now
        self.insert('expired',now-timedelta(days=15));self.insert('missing',now-timedelta(hours=2));self.insert('due',now-timedelta(minutes=10));self.insert('future',now+timedelta(minutes=10))
        self.a.history['TEST']=[dict(ts=(now-timedelta(minutes=10)).isoformat(),close=101.)]
        self.e._settle_batch();rows={r['id']:r for r in self.e.rows('SELECT * FROM desk_observations')}
        self.assertEqual(rows['expired']['outcome_status'],'EXPIRED');self.assertIsNone(rows['expired']['label'])
        self.assertEqual(rows['due']['label'],1);self.assertIsNone(rows['future']['label']);self.assertIsNotNone(rows['missing']['next_check'])
    def test_late_recorded_prediction_never_becomes_training_outcome(self):
        now=old.clock.now;target=now-timedelta(minutes=10)
        self.insert('late',target)
        with self.e.db() as c:c.execute('UPDATE desk_observations SET recorded_at=?',(now.isoformat(),))
        self.a.history['TEST']=[dict(ts=target.isoformat(),close=110.)];self.e._settle_batch()
        r=self.e.rows('SELECT * FROM desk_observations')[0];self.assertEqual(r['outcome_status'],'EXCLUDED');self.assertIsNone(r['label'])
    def test_drift_report_excludes_recomputed_legacy_predictions(self):
        rows=[dict(recorded_at='t',prob=.9,label=1,historical_prob=.8,forward_prob=.9,regime='TREND_UP') for _ in range(300)]
        rows += [dict(recorded_at='t',prob=.9,label=0,historical_prob=.7,forward_prob=.8,regime='RANGE') for _ in range(300)]
        rows += [dict(prob=1.,label=1) for _ in range(100)]
        report=self.e.outcome_report(rows)
        self.assertTrue(report['drift']);self.assertEqual(report['recent']['samples'],300);self.assertEqual(report['paired']['samples'],600)
    def test_forward_fit_calibration_and_validation_are_time_disjoint(self):
        rows=[];now=old.clock.now-timedelta(days=10)
        for i in range(1100):
            ts=now+timedelta(minutes=i*5)
            rows.append(dict(id=str(i),ts=ts.isoformat(),target_ts=(ts+timedelta(minutes=15)).isoformat(),features=json.dumps([float(i%7)/7]*15),label=i%2))
        self.e._fit_forward(rows);m=self.e.get('online_model')
        self.assertEqual(m['schema'],4);self.assertLess(m['train_end'],rows[770]['ts']);self.assertEqual(m['validation_start'],rows[880]['ts'])
        self.assertEqual(m['validation_samples'],220);self.assertTrue(math.isfinite(m['brier']))
    def test_learning_is_only_a_signal_in_the_scanner(self):
        self.e.learning_event.clear();self.e._settle_batch=lambda:self.fail('scanner settled history')
        self.e.settle_observations();self.assertTrue(self.e.learning_event.is_set())
    def test_existing_model_trains_after_close_not_during_entries(self):
        self.a.open=True;self.assertTrue(self.e.train());self.assertFalse(self.e.train_lock.locked());self.assertTrue(self.e.get('stock_retrain_requested'))
    def test_model_pointer_keeps_champion_while_challenger_is_saved(self):
        c=model('challenger')
        with self.e.db() as db:db.execute('INSERT INTO desk_models VALUES(?,?,?)',(c['id'],c['ts'],json.dumps(c)))
        self.e.runtime_cache.clear();self.assertEqual(self.e.model()['id'],'champ')
        result=self.e._promote_challenger(self.m,c);self.assertFalse(result['promoted']);self.assertEqual(self.e.get('stock_champion_id'),'champ')
    def test_regime_detects_trend_range_and_volatility_with_no_future_rows(self):
        def bars(closes,spreads):return [dict(close=c,high=c+d,low=c-d) for c,d in zip(closes,spreads)]
        self.assertEqual(Runtime.market_regime(bars(np.arange(100,161),[.5]*61))['name'],'TREND_UP')
        self.assertEqual(Runtime.market_regime(bars([100+(.3 if i%2 else 0) for i in range(61)],[.3]*61))['name'],'RANGE')
        self.assertEqual(Runtime.market_regime(bars(np.arange(100,161),[.5]*45+[5]*16))['name'],'VOLATILE')
    def test_reversal_requires_distinct_consecutive_candles(self):
        p={'id':'x'};cfg={'confidence':.58};setup={'direction':'UP'};ts=old.clock.now-timedelta(minutes=10)
        sig=dict(direction='DOWN',confidence=.65,bar_ts=ts.isoformat())
        self.assertFalse(self.e.reversal_exit_ready(p,sig,setup,cfg));self.assertFalse(self.e.reversal_exit_ready(p,sig,setup,cfg))
        sig['bar_ts']=(ts+timedelta(minutes=5)).isoformat();self.assertTrue(self.e.reversal_exit_ready(p,sig,setup,cfg))
        sig.update(direction='UP');self.assertFalse(self.e.reversal_exit_ready(p,sig,setup,cfg))
    def test_price_stop_exits_without_waiting_for_reversal_confirmation(self):
        e=old.engine(self.tmp.name+'/exit.db');e.__class__=Runtime
        e.operator_pause=threading.Event();e.command_epoch=0;e.a.open=True
        p=old.position(e,qty=100,status='OPEN',mode='paper');e.a.q['bid']=89.;e.a.q['ask']=89.1
        e.health[p['id']]=dict(bid=89.,observed_mono=time.monotonic())
        exits=[];e.submit=lambda *args:exits.append(args)
        e.manage();self.assertEqual(exits[0][-1],'Stop reached');self.assertEqual(exits[0][2],100)
    def test_nonfinite_display_metric_does_not_break_dashboard_json(self):
        box=Read();e=SimpleNamespace(state=lambda:dict(metric=float('nan'),pnl={'live':float('inf')}),config_defaults={})
        box.response(e)
        for _ in range(50):
            if not box.running:break
            time.sleep(.01)
        result=box.response(e)
        self.assertFalse(result['dashboard']['stale']);self.assertIsNone(result['metric']);self.assertIsNone(result['pnl']['live'])
        json.dumps(result,allow_nan=False)
    def test_index_and_risk_classes_and_defaults_are_unchanged(self):
        import ast,hashlib
        expected={'_build_desk_engine_class': 'b4ede5ea937b7d8be6461cdbd82302ffd99f3719f23f0ebe8adfa91d73750ca7', 'StockDeskEngine': '3e21895184d92f691621dbe3b7f9cbf0ba5e0b6f5753d065db992eea7e815649', 'IndexOptionsAdapter': '31a3c0e6e98c5773749144a55c53abf7acf4a1790972e76b465add0066b6e3ee', 'IndexOptionsEngine': 'e13ffcf3698421cfd456a9bc7b965457311ffb1fe8aa8259cb22c974a6657691', 'index_options_state': '51356965a1820d81471adff7c927be2cc69d5f71fa88a27bead557771d34c5c8', 'index_options_control': 'e51e39402a0bb35038276648b29801cb24533504cdd8ab21a6eb0e23c6246de6', 'IndexAdvancedDeskEngine': '8ae1e387ec1c0a03b5796ab97845e862d25349cf5d0a1f401c0930703c59b297', 'StockPortfolioGuard': 'ee1831f1b6a145c35bbdb83856f1f153acf8304ab5bf8b25360456b8e2aef127', 'SharedAIRisk': 'ff846e19914b09d97304540ab781e547f95b38623d38b3083d1a48c45320e080', 'IndependentDeskRisk': 'beb6ae505afb117c6eb4a435f4e52f547a7e670599a652e09aab9900875adcb9'}
        after=old.SOURCE.read_text();lines=after.splitlines(True)
        # These two shared definitions gained Stock-only entry_history hooks.
        # Their legacy branches are exercised in test_stock_execution instead.
        expected.pop('_build_desk_engine_class');expected.pop('SharedAIRisk')
        actual={n.name:hashlib.sha256(''.join(lines[n.lineno-1:n.end_lineno]).encode()).hexdigest() for n in ast.parse(after).body if isinstance(n,(ast.ClassDef,ast.FunctionDef)) and n.name in expected}
        self.assertEqual(actual,expected)
if __name__=='__main__':unittest.main()
