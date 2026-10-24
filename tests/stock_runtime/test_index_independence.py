"""Independent Index sizing and durable-order recovery, with no external broker."""
import copy
import ast
import json
import sqlite3
import tempfile
import threading
import time
import unittest
from datetime import timedelta
from types import MethodType
import test_stock_runtime as runtime

old=runtime.old
ns=runtime.ns
old.load('IndexOptionsAdapter')
Index=ns['IndexOptionsEngine']
Adapter=ns['IndexOptionsAdapter']

class IndexTests(unittest.TestCase):
    def setUp(self):
        old.clock.now=old.datetime.now(old.IST).replace(tzinfo=None)
        self.tmp=tempfile.TemporaryDirectory();ns['__file__']=self.tmp.name+'/backend.py'
        self.a=runtime.Broker(self.tmp.name+'/index_options_ai_v1.db')
        self.a._Connection=Adapter._Connection;self.a.db=MethodType(Adapter.db,self.a)
        with self.a.db() as c:c.execute('PRAGMA journal_mode=WAL')
        self.e=Index(self.a);self.e._display_risk_at=float('inf');self.a.open=True
        cfg=self.e.config();cfg.update(mode='paper',armed=True,auto_start=False,confidence=.55,square_off='23:59',cooldown=10,capital=100000.)
        self.e.put('config',cfg);self.risk=ns['IndependentDeskRisk']('index',self.e)
        self.m=runtime.model()
        with self.e.db() as c:c.execute('INSERT INTO desk_models VALUES(?,?,?)',(self.m['id'],self.m['ts'],json.dumps(self.m)))
        self.a.spot_key=lambda symbol:'NSE:'+symbol
        self.a.market_quotes=lambda keys,**kw:{key:dict(timestamp=old.clock.now.isoformat(),last_price=100.,ohlc={'open':100.}) for key in keys}
        self.a.q.update(ask=200.,bid=199.9,spread_pct=.05,ask_qty=1000,bid_qty=1000,ask_levels=[dict(price=200.,quantity=1000)],quote_ts=old.clock.now.isoformat())
        self.a.history['NIFTY']=[dict(ts=(old.clock.now-timedelta(minutes=5*(30-i))).isoformat(),close=100.,high=100.5,low=99.5,volume=1000) for i in range(30)]
        self.seed=dict(symbol='NIFTY',ts=old.clock.now.isoformat(),bar_ts=(old.clock.now-timedelta(minutes=5)).isoformat(),bar_age=5.,
            model_id=self.m['id'],decision='READY',direction='UP',p_up=.65,confidence=.65,spot=100.,rank_score=65.,
            contract='TESTCE',exchange='NFO',lot=50,tick=.05,option_type='CE',option_delta=.5,spread=.05)
        self.seed.update(self.plan())
        self.seed.update(decision='READY',reason='Qualified fixture')
        self.e.direction_snapshot=lambda *args,**kwargs:dict(self.seed,decision='CANDIDATE')
        self.e.inspect=lambda symbol:dict(self.seed)
        self.e.signals={'NIFTY':dict(self.seed)};self.e._publish_execution_candidates([self.seed])
    def tearDown(self):self.e.stop_event.set();self.tmp.cleanup()
    def plan(self,remaining=None,funds=None,seed=None):
        return self.e.adaptive_entry_plan(seed or self.seed,self.a.q,50,.05,funds,remaining)
    def recover(self):
        self.e._supervision_retry_at=0;self.e._supervision_next_at=0;self.e.supervise()
    def loss(self,pnl):
        with self.e.db() as c:c.execute('INSERT INTO desk_positions(id,ts,exit_ts,symbol,mode,contract,exchange,lot,qty,entry,stop,target,risk,status,pnl,fees,setup) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            ('past',old.clock.now.isoformat(),old.clock.now.isoformat(),'BANKNIFTY','paper','PASTCE','NFO',50,0,200.,190.,215.,500.,'CLOSED',pnl,0.,'{}'))
    def test_same_plan_at_different_daily_limits_and_headroom(self):
        observed=[]
        for limit,remaining in ((5000.,5000.),(2000.,1000.),(500.,1.)):
            cfg=self.e.config();cfg['daily_loss']=limit;self.e.put('config',cfg)
            plan=self.plan(remaining)
            observed.append(tuple(plan[k] for k in ('entry','stop','target','qty')))
            self.assertIsNone(plan['risk_plan']['risk_budget']);self.assertIsNone(plan['risk_plan']['remaining_daily_risk'])
            self.assertEqual(plan['risk_plan']['sizing_basis'],'trade-independent-v2')
        self.assertEqual(observed,[observed[0]]*3);self.assertGreater(observed[0][-1],0)
    def test_remaining_entry_risk_is_gate_only(self):
        self.loss(-4999.)
        self.assertIsNone(self.e.remaining_entry_risk())
        with self.e.db() as c:c.execute("UPDATE desk_positions SET pnl=-5000 WHERE id='past'")
        with self.assertRaisesRegex(ValueError,'daily loss'):self.e.remaining_entry_risk()
    def test_admission_and_final_repricing_do_not_reduce_size_near_daily_stop(self):
        self.loss(-4999.)
        qty=self.seed['qty'];stop=self.seed['stop'];target=self.seed['target']
        self.e.execute_candidates()
        rows=self.e.active();self.assertEqual(len(rows),1,self.e.execution_status)
        self.assertEqual(rows[0]['qty'],qty);self.assertAlmostEqual(rows[0]['stop'],stop);self.assertAlmostEqual(rows[0]['target'],target)
        setup=json.loads(rows[0]['setup']);self.assertIsNone(setup['risk_plan']['risk_budget'])
        self.assertEqual(len(self.e.rows('SELECT * FROM desk_orders')),1)
        self.assertEqual(self.e.rows('SELECT status FROM desk_orders')[0]['status'],'COMPLETE')
        self.assertEqual(self.a.calls,[])
    def test_daily_breach_blocks_and_latches_without_changing_trade_plan(self):
        self.loss(-5000.);plan=copy.deepcopy(self.seed['risk_plan'])
        self.e.execute_candidates();self.assertEqual(self.e.rows('SELECT * FROM desk_orders'),[])
        self.assertIn('daily loss',self.e.execution_status['reason'])
        self.assertEqual(self.seed['risk_plan'],plan)
        with self.e.db() as c:c.execute("UPDATE desk_positions SET pnl=100 WHERE id='past'")
        self.assertIn('daily loss',self.risk.snapshot('paper')['entry_block'])
    def test_another_positions_planned_loss_does_not_consume_daily_entry_allowance(self):
        p=old.position(self.e,qty=50,status='OPEN',mode='paper')
        with self.e.db() as c:c.execute("UPDATE desk_positions SET symbol='BANKNIFTY',contract='OTHERCE',entry=200,stop=1,target=220 WHERE id=?",(p['id'],))
        self.e.health[p['id']]=dict(bid=200.,observed_mono=time.monotonic())
        view=self.risk.snapshot('paper');self.assertGreater(view['reserved'],5000.);self.assertIsNone(view['entry_block'])
        self.e.execute_candidates()
        self.assertEqual(len(self.e.active()),2,self.e.execution_status)
        new=next(p for p in self.e.active() if p['symbol']=='NIFTY')
        self.assertEqual(new['stop'],self.seed['stop']);self.assertEqual(new['target'],self.seed['target'])
    def test_profit_protection_still_latches(self):
        limits=self.risk.config();self.e.put('independent_risk_config',dict(limits,profit_protection_enabled=True,profit_activation=3000.,profit_giveback=1500.))
        self.loss(4000.);self.assertIsNone(self.risk.snapshot('paper')['reason'])
        with self.e.db() as c:c.execute("UPDATE desk_positions SET pnl=2400 WHERE id='past'")
        view=self.risk.snapshot('paper');self.assertIn('profit giveback',view['entry_block']);self.assertTrue(view['liquidate'])
    def test_cash_and_depth_still_bound_size_without_tightening_stop(self):
        baseline=self.plan();cfg=self.e.config();cfg['capital']=5000.;self.e.put('config',cfg)
        poor=self.plan();self.assertEqual(poor['qty'],0);self.assertEqual(poor['stop'],baseline['stop'])
        cfg['capital']=100000.;self.e.put('config',cfg)
        self.a.q['ask_levels']=[dict(price=200.,quantity=25)]
        shallow=self.plan();self.assertEqual(shallow['qty'],0);self.assertIn('depth',shallow['size_reason'].lower())
    def test_live_available_cash_limits_and_missing_funds_reject(self):
        cfg=self.e.config();cfg.update(mode='live',live_override=True);self.e.put('config',cfg)
        self.assertGreater(self.plan(funds=100000.)['qty'],0)
        self.assertEqual(self.plan(funds=5000.)['qty'],0)
        with self.assertRaisesRegex(ValueError,'available funds'):self.plan(funds=None)
    def test_stop_and_target_respond_to_volatility_and_confidence(self):
        # Make delta * ATR dominate the minimum percentage stop.
        for row in self.a.history['NIFTY']:row.update(high=112.,low=88.)
        lower=self.plan(seed=dict(self.seed,confidence=.60))
        higher=self.plan(seed=dict(self.seed,confidence=.80))
        self.assertLess(higher['stop'],lower['stop'])
        self.assertGreater(higher['target'],lower['target'])
        for row in self.a.history['NIFTY']:row.update(high=114.,low=86.)
        volatile=self.plan(seed=dict(self.seed,confidence=.60))
        self.assertLess(volatile['stop'],lower['stop']);self.assertGreater(volatile['target'],lower['target'])
    def test_pending_order_preserves_legacy_frozen_plan(self):
        p=old.position(self.e);old.order(self.e)
        setup=json.loads(p['setup']);setup['risk_plan'].update(sizing_basis='daily-allocation-v1',risk_budget=500.,initial_stop=90.)
        with self.e.db() as c:c.execute('UPDATE desk_positions SET setup=?',(json.dumps(setup),))
        p=self.e.rows('SELECT * FROM desk_positions')[0]
        self.assertIsNone(self.e.reprice_index_entry(p));self.assertEqual(self.e.rows('SELECT * FROM desk_positions')[0],p)
    def test_unsent_quote_failure_retries_same_signal_without_consuming_trade_limits(self):
        quotes=self.a.quote_batch
        self.a.quote_batch=lambda *args,**kw:{'TESTCE':(None,'Final quote temporarily unavailable')}
        self.e.execute_candidates();self.assertEqual(self.e.rows('SELECT status FROM desk_positions'),[{'status':'CANCELLED'}])
        self.assertEqual(self.e.rows('SELECT * FROM desk_orders'),[]);self.assertEqual(self.e.entry_history('NIFTY'),[])
        self.assertEqual(self.risk.snapshot('paper')['attempts'],0);self.assertIn('Final quote',self.e.execution_status['reason'])
        self.e.execute_candidates();self.assertEqual(len(self.e.rows('SELECT * FROM desk_positions')),1)
        self.e.entry_retry['NIFTY']['at']=0;self.a.quote_batch=quotes;self.e.execute_candidates()
        self.assertEqual(len(self.e.active()),1,self.e.execution_status);self.assertEqual(len(self.e.rows('SELECT * FROM desk_orders')),1)
        self.assertEqual(self.risk.snapshot('paper')['attempts'],1)
    def test_proof_does_not_exclude_any_durable_order(self):
        for status in ('UNKNOWN','SUBMITTING','REJECTED','CANCELLED','COMPLETE'):
            with self.subTest(status=status):
                with self.e.db() as c:c.execute('DELETE FROM desk_orders');c.execute('DELETE FROM desk_positions')
                old.position(self.e,mode='paper',status='CANCELLED');old.order(self.e,status=status)
                self.e.put('index_unsent_plan:p',True)
                self.assertEqual(len(self.e.entry_history('NIFTY')),1)
                self.assertEqual(self.risk.snapshot('paper')['attempts'],1)
    def test_actual_sqlite_writer_recovers_without_disarming_or_creating_duplicate(self):
        writer=sqlite3.connect(self.a.path);writer.execute('BEGIN IMMEDIATE')
        try:
            started=time.monotonic();self.e.supervise();self.assertLess(time.monotonic()-started,3.)
            self.assertTrue(self.e.database_recovery.is_set());self.assertTrue(self.e.config()['armed']);self.assertIsNone(self.e.get('halt'))
            self.assertIn('Index database',self.e.database_status['message'])
            self.e.execute_candidates();self.assertEqual(self.e.rows('SELECT * FROM desk_orders'),[])
        finally:writer.rollback();writer.close()
        self.recover();self.assertFalse(self.e.database_recovery.is_set())
        self.e.execute_candidates();self.e.execute_candidates()
        self.assertEqual(len(self.e.rows('SELECT * FROM desk_orders')),1,self.e.execution_status);self.assertEqual(self.a.calls,[])
    def test_legacy_database_halt_recovers_but_manual_and_daily_halts_remain(self):
        self.a.holdings=lambda:self.fail('Paper DB recovery requested live holdings')
        self.e.put('halt','Position supervision error: database is locked');self.recover();self.assertIsNone(self.e.get('halt'))
        for halt in ('Manual halt','Index daily loss limit reached','Position supervision error: Invalid fill'):
            self.e.put('halt',halt);self.e._database_busy(sqlite3.OperationalError('database is locked'))
            self.recover();self.assertEqual(self.e.get('halt'),halt);self.assertFalse(self.e.database_recovery.is_set())
    def test_lock_after_broker_acceptance_reconciles_without_resubmission(self):
        cfg=self.e.config();cfg.update(mode='live',live_override=True);self.e.put('config',cfg)
        p=old.position(self.e);writers=[];accepted=[]
        self.e.reprice_index_entry=lambda position:(position,100,100.)
        def place(exchange,contract,side,qty,price,tag):
            accepted.append(dict(order_id='ACK-1',tag=tag,tradingsymbol=contract,exchange=exchange,
                transaction_type=side,quantity=qty,product='MIS',status='COMPLETE',filled_quantity=qty,average_price=price))
            writer=sqlite3.connect(self.a.path);writer.execute('BEGIN IMMEDIATE');writers.append(writer)
            return 'ACK-1'
        self.a.place=place
        try:
            with self.assertRaises(sqlite3.OperationalError):self.e.submit(p,'BUY',100,100.,'Test accepted order')
            self.assertEqual(len(accepted),1)
            row=self.e.rows('SELECT * FROM desk_orders')[0]
            self.assertEqual(row['status'],'SUBMITTING');self.assertIsNone(row['broker_id'])
        finally:
            for writer in writers:writer.rollback();writer.close()
        self.a.orders=lambda:accepted
        self.a.holdings=lambda:[dict(product='MIS',exchange='NFO',tradingsymbol='TESTCE',quantity=100)]
        self.e.manage=lambda **kw:None;self.e.maintain_pending_limits=lambda:None
        self.recover();self.recover()
        row=self.e.rows('SELECT * FROM desk_orders')[0]
        self.assertEqual((row['status'],row['broker_id']),('COMPLETE','ACK-1'))
        self.assertEqual(len(accepted),1);self.assertEqual(len(self.e.rows('SELECT * FROM desk_orders')),1)
        self.assertEqual(self.e.active()[0]['qty'],100);self.assertTrue(self.e.config()['armed'])
    def test_interrupted_provably_unsent_plan_recovers_without_permanent_block(self):
        original=self.e.record_trade;writers=[]
        def record(pid,kind,*args,**kwargs):
            if kind=='ENTRY_PLAN':
                writer=sqlite3.connect(self.a.path);writer.execute('BEGIN IMMEDIATE');writers.append(writer)
            return original(pid,kind,*args,**kwargs)
        self.e.record_trade=record
        try:
            with self.assertRaises(sqlite3.OperationalError):self.e.execute_candidates()
            self.assertEqual(self.e.active()[0]['status'],'ENTRY_PENDING')
            self.assertEqual(self.e.rows('SELECT * FROM desk_orders'),[])
        finally:
            for writer in writers:writer.rollback();writer.close()
        self.e.record_trade=original;self.recover()
        self.assertEqual(self.e.active(),[]);self.assertEqual(self.e.entry_history('NIFTY'),[])
        self.e.execute_candidates();self.e.execute_candidates()
        self.assertEqual(len(self.e.rows('SELECT * FROM desk_orders')),1,self.e.execution_status)
        self.assertEqual(self.e.active()[0]['status'],'OPEN');self.assertEqual(self.a.calls,[])
    def test_unknown_legacy_plan_is_not_assumed_unsent(self):
        old.position(self.e)
        self.e._recover_unsent_plans();self.assertEqual(self.e.active()[0]['status'],'ENTRY_PENDING')
        self.e.put('index_durable_intent_v1:p',True);old.order(self.e,status='UNKNOWN')
        self.e._recover_unsent_plans();self.assertEqual(self.e.active()[0]['status'],'ENTRY_PENDING')
        self.assertEqual(len(self.e.rows('SELECT * FROM desk_orders')),1);self.assertEqual(self.a.calls,[])
    def test_unsent_recovery_never_races_entry_worker(self):
        old.position(self.e);self.e.put('index_durable_intent_v1:p',True)
        self.e.entry_lock.acquire()
        try:self.e._recover_unsent_plans();self.assertEqual(len(self.e.active()),1)
        finally:self.e.entry_lock.release()
        self.e._recover_unsent_plans();self.assertEqual(self.e.active(),[])
    def test_partial_fill_survives_repeated_broker_reconciliation(self):
        cfg=self.e.config();cfg.update(mode='live',live_override=True);self.e.put('config',cfg)
        p=old.position(self.e);old.order(self.e)
        row=self.e.rows('SELECT * FROM desk_orders')[0]
        broker=dict(order_id='BROKER-1',tag=row['tag'],tradingsymbol=p['contract'],exchange=p['exchange'],
            transaction_type='BUY',quantity=100,product='MIS',status='CANCELLED',filled_quantity=50,average_price=100.)
        self.a.orders=lambda:[broker];self.a.holdings=lambda:[dict(product='MIS',exchange='NFO',tradingsymbol='TESTCE',quantity=50)]
        self.e.manage=lambda **kw:None;self.e.maintain_pending_limits=lambda:None
        self.recover();self.recover()
        self.assertEqual((self.e.active()[0]['status'],self.e.active()[0]['qty']),('OPEN',50));self.assertEqual(self.a.calls,[])
    def test_database_recovery_preserves_concurrent_daily_halt_and_manual_disarm(self):
        self.e.put('halt','Position supervision error: database is locked')
        self.e.manage=lambda **kw:self.e.put('halt','Index daily loss limit reached')
        self.e.control('disarm',{});self.recover()
        self.assertEqual(self.e.get('halt'),'Index daily loss limit reached');self.assertFalse(self.e.config()['armed'])
    def test_index_writer_is_independent_of_stock_queue(self):
        stock=runtime.Adapter._Connection.writer
        self.assertIsNot(stock,Adapter._Connection.writer)
        stock.acquire()
        try:
            started=time.monotonic();self.e.put('index_test',True);self.assertLess(time.monotonic()-started,.5)
        finally:stock.release()
    def test_stock_and_index_config_and_limits_remain_separate(self):
        stock=old.engine(self.tmp.name+'/stock.db');ns['IndependentDeskRisk']('stock',stock)
        cfg=self.e.config();cfg.update(daily_loss=1234.,capital=80000.);self.e.put('config',cfg)
        self.assertNotEqual(stock.config()['daily_loss'],self.e.config()['daily_loss'])
        self.assertIsNot(stock.shared_risk.lock,self.risk.lock)
    def test_submitted_or_unknown_orders_are_not_retried(self):
        old.position(self.e,mode='live');old.order(self.e,status='UNKNOWN')
        self.e.execute_candidates();self.e.execute_candidates()
        self.assertEqual(len(self.e.rows('SELECT * FROM desk_orders')),1);self.assertEqual(self.a.calls,[])
    def test_new_entry_mode_keeps_manual_disarm(self):
        self.e.control('disarm',{});self.e.execute_candidates()
        self.assertFalse(self.e.config()['armed']);self.assertEqual(self.e.rows('SELECT * FROM desk_orders'),[])
    def test_broker_credentials_require_environment_and_have_no_literal_secret(self):
        from types import SimpleNamespace
        nodes=[n for n in old.tree.body if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id in ('API_KEY','API_SECRET') for t in n.targets)]
        self.assertEqual(len(nodes),2)
        for env,expected in (({},('','')),({'KITE_API_KEY':'fixture-key','KITE_API_SECRET':'fixture-secret'},('fixture-key','fixture-secret'))):
            scope={'os':SimpleNamespace(environ=env)}
            exec(compile(ast.Module(body=nodes,type_ignores=[]),'<credential configuration>','exec'),scope)
            self.assertEqual((scope['API_KEY'],scope['API_SECRET']),expected)

if __name__=='__main__':unittest.main()
