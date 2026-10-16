"""Stock execution/retry regressions with real SQLite ledgers and fake market data."""
import ast
import json
import secrets
import sqlite3
import threading
import time
import unittest
from datetime import datetime,timedelta
from unittest.mock import patch
import test_stock_runtime as runtime

old=runtime.old
ns=runtime.ns
for node in old.tree.body:
    if isinstance(node,ast.Assign) and any(isinstance(t,ast.Name) and t.id.startswith('STOCK_') for t in node.targets):
        try:ast.literal_eval(node.value)
        except (ValueError,TypeError):continue
        exec(compile(ast.Module(body=[node],type_ignores=[]),'<stock constants>','exec'),ns)

class ExecutionTests(unittest.TestCase):
    def setUp(self):
        old.clock.now=datetime.now(old.IST).replace(tzinfo=None)
        runtime.RuntimeTests.setUp(self)
        self.a.open=True;self.a.symbols=('TEST',)
        cfg=self.e.config();cfg.update(mode='paper',armed=True,auto_start=False,confidence=.55,square_off='23:59',cooldown=10)
        self.e.put('config',cfg)
        self.risk=ns['IndependentDeskRisk']('stock',self.e)
        self.e.portfolio_guard.sectors=lambda:{'TEST':'TEST INDUSTRY'}
        self.a.spot_key=lambda symbol:'NSE:'+symbol
        self.a.market_quotes=lambda keys,**kw:{key:dict(timestamp=old.clock.now.isoformat(),last_price=100.,ohlc={'open':100.}) for key in keys}
        self.a.q['quote_ts']=old.clock.now.isoformat()
        self.a.history['TEST']=[dict(ts=(old.clock.now-timedelta(minutes=5*(30-i))).isoformat(),close=100.,high=100.5,low=99.5,volume=1000) for i in range(30)]
        self.seed=dict(symbol='TEST',ts=old.clock.now.isoformat(),bar_ts=(old.clock.now-timedelta(minutes=5)).isoformat(),bar_age=5.,
            model_id='champ',decision='READY',direction='UP',p_up=.7,confidence=.7,spot=100.,rank_score=70.,
            contract='TESTCE',exchange='NFO',lot=50,tick=.05,option_type='CE',option_delta=.5,spread=.1)
        self.seed.update(self.e.adaptive_entry_plan(self.seed,self.a.q,50,.05,None,None))
        self.seed.update(decision='READY',reason='Qualified fixture')
        self.e.direction_snapshot=lambda *args,**kwargs:dict(self.seed,decision='CANDIDATE')
        self.e.inspect=lambda symbol:dict(self.seed)
        self.e.signals={'TEST':dict(self.seed)}
        self.e._publish_execution_candidates([self.seed])
    def tearDown(self):runtime.RuntimeTests.tearDown(self)
    def test_qualifying_paper_entry_creates_one_fill_without_broker_order(self):
        self.e.execute_candidates()
        rows=self.e.active();self.assertEqual(len(rows),1,self.e.execution_status)
        self.assertEqual(rows[0]['status'],'OPEN');self.assertGreater(rows[0]['qty'],0)
        orders=self.e.rows('SELECT * FROM desk_orders');self.assertEqual(len(orders),1)
        self.assertEqual(orders[0]['status'],'COMPLETE');self.assertEqual(orders[0]['mode'],'paper')
        self.assertEqual(self.a.calls,[])
        self.e.execute_candidates();self.assertEqual(len(self.e.rows('SELECT * FROM desk_orders')),1)
    def test_slow_history_does_not_hold_paper_entry_or_display(self):
        started=threading.Event();release=threading.Event()
        self.a.refresh=lambda symbol:(started.set(),release.wait(2))
        self.e._queue_history(['TEST'])
        thread=threading.Thread(target=self.e._refresh_history_once);thread.start();self.assertTrue(started.wait(1))
        try:
            self.e.execute_candidates();self.assertEqual(len(self.e.active()),1,self.e.execution_status)
            state=self.e.state();self.assertEqual(len(state['positions']),1)
            self.assertFalse(release.is_set())
        finally:release.set();thread.join(2)
    def test_unsent_final_quote_failure_retries_same_signal_without_cooldown(self):
        quote=self.a.quote_batch
        self.a.quote_batch=lambda *args,**kwargs:{'TESTCE':(None,'Temporary final quote read failed')}
        self.e.execute_candidates()
        self.assertEqual(self.e.rows('SELECT status FROM desk_positions'),[{'status':'CANCELLED'}])
        self.assertEqual(self.e.rows('SELECT * FROM desk_orders'),[])
        self.assertIn('Temporary final quote',self.e.execution_status['reason'])
        self.assertEqual(self.e.entry_history('TEST'),[])
        self.a.quote_batch=quote
        self.e.execute_candidates();self.assertEqual(len(self.e.rows('SELECT * FROM desk_positions')),1)  # bounded backoff
        self.e.entry_retry['TEST']['at']=0
        self.e.execute_candidates()
        self.assertEqual(len(self.e.active()),1,self.e.execution_status)
        self.assertEqual(len(self.e.rows('SELECT * FROM desk_positions')),2)  # failed plan retained
        self.assertEqual(len(self.e.rows('SELECT * FROM desk_orders')),1)
        self.assertEqual(self.a.calls,[])
    def test_any_durable_order_keeps_same_signal_lock(self):
        for state in ('UNKNOWN','SUBMITTING','REJECTED','CANCELLED','COMPLETE'):
            with self.subTest(state=state):
                with self.e.db() as c:c.execute('DELETE FROM desk_orders');c.execute('DELETE FROM desk_positions')
                old.position(self.e,mode='paper',status='CANCELLED',qty=0)
                with self.e.db() as c:c.execute('UPDATE desk_positions SET symbol=?,ts=?,setup=?,pnl=0,fees=0',('TEST',old.clock.now.isoformat(),json.dumps(dict(self.seed,shared_signal_key=self.seed['bar_ts']))))
                old.order(self.e,status=state)
                self.e.put('stock_unsent_plan:p',True)  # any later intent overrides even prior unsent evidence
                self.assertEqual(len(self.e.entry_history('TEST')),1)
                self.assertIn('Signal already used',self.risk.admit(self.e,dict(self.seed)))
    def test_old_cancelled_record_without_unsent_proof_is_not_silently_bypassed(self):
        old.position(self.e,mode='paper',status='CANCELLED',qty=0)
        with self.e.db() as c:c.execute("UPDATE desk_positions SET symbol='TEST',pnl=0,fees=0")
        self.assertEqual(len(self.e.entry_history('TEST')),1)
    def test_manual_disarm_is_visible_and_never_auto_rearmed(self):
        self.e.control('disarm',{})
        for _ in range(3):self.e.execute_candidates()
        self.assertEqual(self.e.execution_status['status'],'PAUSED')
        self.assertIn('disarmed',self.e.execution_status['reason'])
        self.assertEqual(self.e.rows('SELECT * FROM desk_orders'),[])
        self.assertFalse(self.e.config()['armed'])
    def test_daily_loss_halt_still_blocks_paper_entries(self):
        self.e.put('halt','Daily loss limit reached')
        self.e.execute_candidates()
        self.assertIn('Daily loss',self.e.execution_status['reason']);self.assertEqual(self.e.active(),[])
    def test_execution_worker_retries_without_new_scanner_event(self):
        attempted=threading.Event();count=[]
        def execute():
            count.append(1)
            if len(count)>=2:attempted.set()
        self.e.execute_candidates=execute;self.e.cycle=lambda:None;self.e._risk_tick=lambda:None;self.e._learn_once=lambda:None
        self.e.execution_event.clear();self.e.start()
        try:self.assertTrue(attempted.wait(3.5))
        finally:
            self.e.stop_event.set()
            for event in (self.e.scan_event,self.e.history_event,self.e.risk_event,self.e.execution_event,self.e.learning_event):event.set()
            time.sleep(.05)
    def test_scan_publishes_fresh_candidate_without_waiting_for_history(self):
        self.a.symbols=('TEST','STALE')
        self.e._sync_full_universe_membership=lambda:None;self.e._ensure_deep_history_backfill=lambda:None
        self.e.ban_checked=time.monotonic()
        self.e._screen_direction=lambda symbol:dict(self.seed,symbol=symbol,decision='CANDIDATE' if symbol=='TEST' else 'WAIT',bar_age=5 if symbol=='TEST' else 30)
        self.e._refresh_batch=lambda *args:['STALE']
        self.e.maybe_train=lambda:None;self.e.request_success_training=lambda:None;self.e._ensure_historical_success_training=lambda:None
        self.a.refresh=lambda symbol:self.fail('Scanner waited for a network history read')
        self.e.supervise=lambda:self.fail('Scanner waited for broker supervision')
        self.e.cycle()
        self.assertEqual(self.e.scan_status['status'],'COMPLETE',self.e.scan_status)
        self.assertEqual(self.e.history_queue,['STALE'])
        self.assertTrue(self.e.execution_event.is_set());self.assertEqual(self.e.execution_candidates[0]['symbol'],'TEST')
    def test_stale_ready_rows_and_disarmed_rows_are_not_shown_as_executable(self):
        old_seed=dict(self.seed,ts=(old.clock.now-timedelta(minutes=2)).isoformat())
        rows=self.e.display_signals([old_seed],self.e.config(),[],None)
        self.assertEqual(rows[0]['decision'],'WAIT');self.assertIn('recheck',rows[0]['reason'])
        rows=self.e.display_signals([dict(self.seed)],self.e.config(),[],'Entries disarmed')
        self.assertTrue(rows[0]['setup_qualified']);self.assertEqual(rows[0]['decision'],'WAIT');self.assertIn('disarmed',rows[0]['reason'])
    def test_current_display_counts_are_from_same_signals_as_table(self):
        self.e.risk_display=dict(timestamp=old.clock.now.isoformat(),states={'paper':{'entry_block':None}})
        self.e.scan_status=dict(ready=0,fresh_universe=0)
        s=self.e.state();self.assertEqual(s['scan']['ready'],1);self.assertEqual(s['scan']['fresh_universe'],1)
        self.e.signals['TEST']['ts']=(old.clock.now-timedelta(minutes=2)).isoformat()
        s=self.e.state();self.assertEqual(s['scan']['ready'],0);self.assertEqual(s['signals'][0]['decision'],'WAIT')
    def test_index_has_no_stock_history_filter(self):
        index=old.engine(self.tmp.name+'/index.db','index')
        self.assertFalse(hasattr(index,'entry_history'))
    def test_auth_read_is_nonwriting_and_revocation_is_still_immediate(self):
        local=dict(sqlite3=sqlite3,secrets=secrets,_SITE_AUTH_DB=self.tmp.name+'/auth.db')
        node=next(n for n in old.tree.body if isinstance(n,ast.FunctionDef) and n.name=='_site_generation')
        exec(compile(ast.Module(body=[node],type_ignores=[]),'<auth>','exec'),local)
        read=local['_site_generation'];generation=read()
        writer=sqlite3.connect(local['_SITE_AUTH_DB']);writer.execute('BEGIN IMMEDIATE')
        try:
            started=time.monotonic();self.assertEqual(read(),generation);self.assertLess(time.monotonic()-started,.2)
        finally:writer.rollback();writer.close()
        changed=read(revoke=True);self.assertNotEqual(changed,generation);self.assertEqual(read(),changed)

if __name__=='__main__':unittest.main()
