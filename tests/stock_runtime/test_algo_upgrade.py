"""Run production methods against SQLite and fake brokers; never start the app."""
import ast
import json
import math
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
import numpy as np
try:
    import requests
except ModuleNotFoundError:
    from pip._vendor import requests

IST=timezone(timedelta(hours=5,minutes=30))
clock=SimpleNamespace(now=datetime.now(IST).replace(tzinfo=None))
SOURCE=Path(__file__).with_name('backend.py')
if not SOURCE.exists():SOURCE=Path(__file__).resolve().parents[2]/'backend.py'
tree=ast.parse(SOURCE.read_text())
ns=dict(np=np,json=json,math=math,os=os,sqlite3=sqlite3,threading=threading,time=time,
    contextmanager=contextmanager,datetime=datetime,timedelta=timedelta,timezone=timezone,
    IST=IST,requests=requests,now_ist=lambda:clock.now,SESSION={'access_token':'FAKE'},
    API_KEY='FAKE',AI_SYMBOLS=['NIFTY'],INDEX_OPTION_EXCHANGE={},STOCK_HISTORY_BACKFILL_DAYS=365,
    _serialized_training=lambda fn:fn,BrokerDeferred=type('BrokerDeferred',(RuntimeError,),{}))
def load(name):
    node=next(n for n in tree.body if isinstance(n,(ast.ClassDef,ast.FunctionDef)) and n.name==name)
    exec(compile(ast.Module(body=[node],type_ignores=[]),'<production '+name+'>','exec'),ns)
for name in ['_ai_ts_naive','_build_desk_engine_class','desk_fit_calibration','desk_validation_report','desk_blend_weights','DeskMarketFeed']:
    load(name)
ns['DeskEngine']=ns['_build_desk_engine_class']()
assignment=next(n for n in tree.body if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='DESK_EXECUTION_DEFAULTS' for t in n.targets))
exec(compile(ast.Module(body=[assignment],type_ignores=[]),'<production defaults>','exec'),ns)
for name in ['DeskAdapter','StockDeskAdapter','StockDeskEngine','IndexOptionsEngine','StockPortfolioGuard','SharedAIRisk','IndependentDeskRisk']:load(name)
portfolio_defaults=next(n for n in tree.body if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='STOCK_PORTFOLIO_DEFAULTS' for t in n.targets))
exec(compile(ast.Module(body=[portfolio_defaults],type_ignores=[]),'<portfolio defaults>','exec'),ns)

class FakeBroker:
    symbols=('NIFTY',)
    def __init__(self,path):
        self.path=path;self.calls=[];self.fail_modify=False;self.place_error=None
        self.q=dict(ask=100.10,bid=100.,spread_pct=.1,ask_qty=500,bid_qty=500,
            ask_levels=[{'price':100.10,'quantity':500}],quote_ts=clock.now.isoformat())
    @contextmanager
    def db(self):
        c=sqlite3.connect(self.path);c.row_factory=sqlite3.Row
        try:yield c;c.commit()
        except Exception:c.rollback();raise
        finally:c.close()
    def quote_batch(self,ex,contracts,bypass_cache=False):return {c:(dict(self.q),None) for c in contracts}
    def market_open(self):return True
    def connected(self):return True
    def cancel(self,bid):self.calls.append(('cancel',bid))
    def modify(self,bid,price):
        self.calls.append(('modify',bid,price))
        if self.fail_modify:raise requests.exceptions.ConnectionError('response lost')
        return bid
    def place(self,*args):
        self.calls.append(('place',)+args)
        if self.place_error:raise self.place_error
        return 'BROKER-1'
    def bars(self,symbol,n):return []

def engine(path,desk='stock'):
    cls=ns['StockDeskEngine'] if desk=='stock' else ns['IndexOptionsEngine']
    e=object.__new__(cls);e.a=FakeBroker(path);e.lock=threading.RLock();e.risk_lock=threading.RLock()
    e.signals={};e.health={};e.mismatches=set();e.record_cache={};e.recorder_error=None
    e.training={'status':'IDLE'};e.train_lock=threading.Lock();e.desk_name=desk;e.init_db()
    e.entry_lock=threading.Lock();e.candidate_lock=threading.Lock();e.execution_event=threading.Event()
    e.risk_event=threading.Event();e.execution_candidates=[];e.execution_generation=0;e.execution_status={}
    e.put('config',dict(armed=True,mode='live',max_hold=360,capital=100000.,max_spread=1.,adaptive_risk=True,square_off='23:59'))
    e.available_funds=lambda force=False:1000000.
    e.check_underlying=lambda s:{'price':100.}
    e.adaptive_position_update=lambda *args:None
    view=dict(reason=None,entry_block=None,liquidate=False,remaining=4000.,positions=1,
        desk_totals={desk:0.},desk_reserved={desk:1000.},config={'max_positions':3})
    e.shared_risk=SimpleNamespace(lock=threading.RLock(),snapshot=lambda mode:dict(view),confidence_block=lambda *args:None)
    return e

def position(e,qty=0,status='ENTRY_PENDING',mode='live'):
    setup=dict(qty=100,entry=100.,stop=90.,target=120.,lot=50,tick=.05,model_id='m',
        symbol='NIFTY',direction='UP',confidence=.65,bar_ts=clock.now.isoformat(),
        risk_plan={'capital_allocation':100000.,'initial_stop':90.,'policy':'adaptive-v1'})
    with e.db() as c:
        c.execute('INSERT INTO desk_positions(id,ts,symbol,mode,contract,exchange,lot,qty,entry,stop,target,risk,status,setup) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            ('p',(clock.now-timedelta(seconds=10)).isoformat(),'NIFTY',mode,'TESTCE','NFO',50,qty,100. if qty else 0.,90.,120.,1000.,status,json.dumps(setup)))
    return e.rows('SELECT * FROM desk_positions')[0]

def order(e,side='BUY',status='OPEN',filled=0,qty=100,age=10):
    with e.db() as c:
        c.execute('INSERT INTO desk_orders(id,ts,position_id,side,qty,mode,tag,broker_id,status,filled,notional,limit_price,reason) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
            ('o',(clock.now-timedelta(seconds=age)).isoformat(),'p',side,qty,'live','TEST','BROKER-1',status,filled,filled*100.,100.,'Target reached' if side=='SELL' else 'Qualified'))

class UpgradeTests(unittest.TestCase):
    def setUp(self):
        clock.now=datetime.now(IST).replace(tzinfo=None)
        self.tmp=tempfile.TemporaryDirectory();self.e=engine(self.tmp.name+'/stock.db')
    def tearDown(self):self.tmp.cleanup()
    def test_unknown_order_never_modified_or_replayed(self):
        position(self.e);order(self.e,status='UNKNOWN');self.e.maintain_pending_limits()
        self.assertEqual(self.e.a.calls,[]);self.assertEqual(len(self.e.rows('SELECT * FROM desk_orders')),1)
    def test_entry_ttl_requests_cancel_without_fabricating_status(self):
        position(self.e);order(self.e,age=50);self.e.maintain_pending_limits()
        self.assertEqual(self.e.a.calls,[('cancel','BROKER-1')])
        self.assertEqual(self.e.rows('SELECT status FROM desk_orders')[0]['status'],'OPEN')
    def test_known_limit_is_modified_on_same_id_with_price_cap(self):
        position(self.e);order(self.e);self.e.maintain_pending_limits()
        self.assertEqual(self.e.a.calls[0][:2],('modify','BROKER-1'))
        self.assertLessEqual(self.e.a.calls[0][2],100.15)
        self.assertEqual(self.e.rows('SELECT qty FROM desk_orders')[0]['qty'],100)
    def test_modify_timeout_waits_for_same_id_authoritative_read(self):
        position(self.e);order(self.e);self.e.a.fail_modify=True;self.e.maintain_pending_limits()
        clock.now+=timedelta(seconds=5);self.e.maintain_pending_limits()
        self.assertEqual(len(self.e.a.calls),1)
        self.e.apply_order('o',dict(order_id='BROKER-1',status='OPEN',filled_quantity=0,average_price=0,price=100.15))
        self.assertFalse(self.e.get('execution_controller:o')['uncertain'])
        self.assertEqual(self.e.rows('SELECT limit_price FROM desk_orders')[0]['limit_price'],100.15)
    def test_entry_chase_disabled_prevents_price_increase(self):
        position(self.e);order(self.e);cfg=self.e.config();cfg['entry_chase_bps']=0;self.e.put('config',cfg)
        self.e.maintain_pending_limits();self.assertEqual(self.e.a.calls,[])
    def test_mismatch_prevents_modification(self):
        position(self.e);order(self.e);self.e.mismatches={'p'};self.e.maintain_pending_limits();self.assertEqual(self.e.a.calls,[])
    def test_index_pending_price_cannot_exceed_original_risk_plan(self):
        e=engine(self.tmp.name+'/index.db','index');position(e);order(e);e.maintain_pending_limits()
        self.assertEqual(e.a.calls,[])
    def test_cancelled_partial_buy_preserves_actual_fills_and_records_delay(self):
        position(self.e);order(self.e)
        self.e.apply_order('o',dict(order_id='BROKER-1',status='CANCELLED',filled_quantity=50,average_price=99.9))
        p=self.e.rows('SELECT * FROM desk_positions')[0]
        self.assertEqual((p['status'],p['qty']),('OPEN',50));self.assertEqual(p['entry'],99.9)
        self.assertIsNotNone(self.e.get('first_fill:p'))
        self.assertLess(self.e.get('execution_fill:o')['slippage_vs_limit_bps'],0)
    def test_confirmed_exit_remainder_is_durable_and_resubmitted_only_after_delay(self):
        position(self.e,qty=100,status='EXIT_PENDING');order(self.e,side='SELL',qty=100)
        self.e.apply_order('o',dict(order_id='BROKER-1',status='CANCELLED',filled_quantity=50,average_price=100.))
        self.assertEqual(self.e.get('exit_resume:p')['qty'],50)
        calls=[];self.e.submit=lambda *args:calls.append(args)
        self.e.manage();self.assertEqual(calls,[])
        clock.now+=timedelta(seconds=6);self.e.a.q['quote_ts']=datetime.now(IST).isoformat();self.e.manage()
        self.assertEqual(len(calls),1);self.assertEqual(calls[0][2],50)
    def test_old_reversal_cannot_close_current_position(self):
        position(self.e,qty=50,status='OPEN');self.e.signals['NIFTY']=dict(direction='DOWN',confidence=.8,bar_age=5,bar_ts=(clock.now-timedelta(minutes=40)).isoformat())
        calls=[];self.e.submit=lambda *args:calls.append(args);self.e.manage();self.assertEqual(calls,[])
    def test_fresh_reversal_can_close_position(self):
        position(self.e,qty=50,status='OPEN');self.e.signals['NIFTY']=dict(direction='DOWN',confidence=.8,bar_age=5,bar_ts=(clock.now-timedelta(minutes=5)).isoformat())
        calls=[];self.e.submit=lambda *args:calls.append(args);self.e.manage()
        self.assertEqual(calls[0][-1],'Direction reversed')
    def test_worker_recovers_after_exception_and_logging_failure(self):
        self.e.event=lambda *args:(_ for _ in ()).throw(ValueError('log unavailable'))
        self.assertFalse(self.e._worker_tick('execution',lambda:(_ for _ in ()).throw(ValueError('worker error'))))
        calls=[];self.assertTrue(self.e._worker_tick('execution',lambda:calls.append(1)));self.assertEqual(calls,[1])
    def test_candidate_handoff_wakes_and_filters_already_owned(self):
        position(self.e,qty=50,status='OPEN');self.e.a.symbols=('NIFTY','BANKNIFTY')
        self.e._publish_execution_candidates([dict(symbol='NIFTY',decision='READY',rank_score=99),dict(symbol='BANKNIFTY',decision='READY',rank_score=90)])
        self.assertEqual([x['symbol'] for x in self.e.execution_candidates],['BANKNIFTY']);self.assertTrue(self.e.execution_event.is_set())
    def test_settings_are_saved_separately(self):
        e2=engine(self.tmp.name+'/index.db','index')
        for e in [self.e,e2]:cfg=e.config();cfg['armed']=False;e.put('config',cfg)
        self.e.save_config({'entry_chase_bps':25,'forward_weight_cap':.25})
        self.assertEqual(self.e.config()['entry_chase_bps'],25);self.assertEqual(e2.config()['entry_chase_bps'],15)
        self.assertEqual(e2.config()['forward_weight_cap'],.4)
    def test_settings_reject_nonfinite_and_out_of_bounds(self):
        cfg=self.e.config();cfg['armed']=False;self.e.put('config',cfg)
        for data in [{'entry_ttl_seconds':float('nan')},{'forward_weight_cap':.5},{'entry_max_modifications':1.5}]:
            with self.assertRaises(ValueError):self.e.save_config(data)
    def test_fresh_stream_preserves_exchange_timestamp_and_invalidates_on_disconnect(self):
        feed=ns['DeskMarketFeed']();ws=object();feed.ws=ws;feed.token='FAKE';feed.connected=True
        feed.remember({'NFO:TESTCE':{'instrument_token':123}})
        stamp=clock.now.replace(tzinfo=IST).astimezone(timezone.utc)
        feed._ticks(ws,[{'instrument_token':123,'exchange_timestamp':stamp,'last_price':100.,'depth':{'buy':[],'sell':[]}}])
        q=feed.read(['NFO:TESTCE'])['NFO:TESTCE'];self.assertEqual(q['timestamp'],clock.now.isoformat())
        feed._invalidate(ws,'Disconnected');self.assertEqual(feed.read(['NFO:TESTCE']),{})
    def test_stale_out_of_order_and_old_socket_ticks_are_not_used(self):
        feed=ns['DeskMarketFeed']();ws=object();feed.ws=ws;feed.token='FAKE';feed.connected=True;feed.remember({'NFO:TESTCE':{'instrument_token':1}})
        tick={'instrument_token':1,'exchange_timestamp':clock.now.replace(tzinfo=IST),'last_price':100.}
        feed._ticks(ws,[tick]);feed._ticks(ws,[dict(tick,exchange_timestamp=(clock.now-timedelta(seconds=5)).replace(tzinfo=IST),last_price=90.)])
        self.assertEqual(feed.read(['NFO:TESTCE'])['NFO:TESTCE']['last_price'],100.)
        clock.now+=timedelta(seconds=4);self.assertEqual(feed.read(['NFO:TESTCE']),{})
        feed._ticks(object(),[tick]);self.assertEqual(feed.read(['NFO:TESTCE']),{})
    def test_forward_weight_is_soft_bounded_and_stale_forward_gets_zero(self):
        h=dict(auc=.52,brier=.248,validation_samples=1000,ts=clock.now.isoformat())
        f=dict(auc=.60,brier=.22,validation_samples=1000,ts=clock.now.isoformat())
        blend=ns['desk_blend_weights'](h,f)
        self.assertGreater(blend['forward_weight'],.2);self.assertLessEqual(blend['forward_weight'],.4)
        self.assertAlmostEqual(blend['historical_weight']+blend['forward_weight'],1.)
        f['ts']=(clock.now-timedelta(days=8)).isoformat();self.assertEqual(ns['desk_blend_weights'](h,f)['forward_weight'],0)
    def test_calibration_is_monotone_bounded_and_requires_two_classes(self):
        p=np.linspace(.1,.9,1000);y=(np.arange(1000)%2).astype(float)
        c=ns['desk_fit_calibration'](p,y);self.assertTrue(c['enabled']);self.assertTrue(.25<=c['scale']<=2.)
        self.assertFalse(ns['desk_fit_calibration'](p,np.zeros(1000))['enabled'])
        report=ns['desk_validation_report'](p,y);self.assertEqual(sum(x['count'] for x in report['reliability_bins']),1000)
        self.assertEqual(sum(x['samples'] for x in report['chronological_windows']),1000)
    def test_network_loss_during_submit_keeps_unknown_intent_once(self):
        p=position(self.e);self.e.a.place_error=requests.exceptions.ConnectionError('lost acknowledgement')
        ns['DeskEngine'].submit(self.e,p,'BUY',100,100.,'Qualified')
        ns['DeskEngine'].submit(self.e,p,'BUY',100,100.,'Qualified')
        self.assertEqual(sum(c[0]=='place' for c in self.e.a.calls),1)
        self.assertEqual(self.e.rows('SELECT status FROM desk_orders')[0]['status'],'UNKNOWN')
    def test_index_reprices_unsent_intent_instead_of_rejecting_old_limit(self):
        e=engine(self.tmp.name+'/index.db','index');p=position(e)
        setup=json.loads(p['setup']);setup['option_delta']=.5;p['setup']=json.dumps(setup)
        with e.db() as c:c.execute('UPDATE desk_positions SET setup=? WHERE id=?',(p['setup'],'p'))
        bars=[dict(ts=(clock.now-timedelta(minutes=5*(21-i))).isoformat(),high=102.,low=98.,close=100.) for i in range(20)]
        e.a.bars=lambda symbol,n:bars
        e.model_quality=lambda:dict(live_ok=True,risk_weight=1.,issues=[])
        e._apply_success_intelligence=lambda s:s;e.entry_quality_gate=lambda s:True
        refreshed,qty,price=e.reprice_index_entry(p)
        self.assertGreater(price,100.)  # Old limit was below today's ask.
        self.assertGreater(qty,0);self.assertEqual(qty%50,0)
        self.assertEqual(refreshed['qty'],0);self.assertEqual(e.rows('SELECT * FROM desk_orders'),[])
        self.assertIsNotNone(json.loads(refreshed['setup'])['execution_reprice']['quote_ts'])
    def test_index_reprice_retains_other_position_valuation_gate(self):
        e=engine(self.tmp.name+'/index.db','index');p=position(e)
        e.model_quality=lambda:dict(live_ok=True,risk_weight=1.,issues=[])
        e.shared_risk.snapshot=lambda mode:dict(reason=None,entry_block='Fresh position quotes required: OTHER')
        with self.assertRaisesRegex(ValueError,'Fresh position quotes'):e.reprice_index_entry(p)
    def test_training_purges_label_horizons_around_calibration_and_final_holdout(self):
        names=['ret_1','ret_3','ret_5','ret_10','ret_20','ema_gap_9_20','ema_gap_20_50','rsi14','volatility_10','range_20','volume_ratio','trend_score','momentum_score','time_sin','time_cos']
        bars=[]
        for i in range(6000):
            price=100+math.sin(i/8.)
            bars.append(dict(ts=(clock.now-timedelta(minutes=5*(6001-i))).isoformat(),close=price,high=price+1,low=price-1,volume=100))
        self.e.a.bars=lambda symbol,n:bars[-n:]
        self.e.a.features=lambda rows,i,arr:{name:math.sin(i/(8+j)) for j,name in enumerate(names)}
        self.e.train_lock.acquire();self.e._train()
        self.assertEqual(self.e.training['status'],'COMPLETE',self.e.training)
        model=self.e.model();parse=ns['_ai_ts_naive']
        self.assertLess(parse(model['train_end']),parse(model['calibration_start']))
        self.assertLess(parse(model['calibration_end']),parse(model['validation_start']))
        self.assertGreaterEqual(model['calibration_samples'],100);self.assertTrue(model['calibration']['enabled'])
        self.assertEqual(sum(x['samples'] for x in model['validation_report']['chronological_windows']),model['validation_samples'])
    def test_stock_adapter_uses_rest_when_stream_snapshot_expired(self):
        adapter=object.__new__(ns['StockDeskAdapter']);feed=ns['DeskMarketFeed']()
        ns['DESK_MARKET_FEED']=feed;calls=[]
        def rest(keys,**kwargs):
            calls.append(keys);return {key:dict(instrument_token=123,timestamp=clock.now.isoformat()) for key in keys}
        ns['kite_quote_bulk']=rest
        raw=adapter.market_quotes(['NFO:TESTCE'],bypass_cache=True)
        self.assertEqual(calls,[['NFO:TESTCE']]);self.assertIn('NFO:TESTCE',raw)
        self.assertEqual(feed.token_keys[123],'NFO:TESTCE')
    def test_portfolio_preentry_uses_current_adapter_feed(self):
        guard=ns['StockPortfolioGuard'](self.e);guard.config=lambda:dict(max_same_direction=2,max_same_sector=1,max_correlation=.75)
        guard.sectors=lambda:{'NIFTY':'OTHER'}
        self.e.a.spot_key=lambda symbol:'NSE:'+symbol
        calls=[]
        def quotes(keys,**kwargs):
            calls.append(keys);return {key:dict(timestamp=clock.now.isoformat(),last_price=100.,ohlc={'open':100.}) for key in keys}
        self.e.a.market_quotes=quotes
        context=guard.prepare(dict(symbol='NIFTY',contract='TESTCE'))
        self.assertEqual(context['side'],'CE');self.assertIn('NSE:NIFTY 50',calls[0])
    def test_one_entry_per_arm_disarms_after_the_attempt(self):
        self.e.block=lambda:None
        cfg=self.e.config();cfg.update(mode='paper',adaptive_risk=False,cooldown=0);self.e.put('config',cfg)
        self.e.shared_risk.config=lambda:dict(one_trade_per_arm=True)
        self.e.shared_risk.admit=lambda *args:None
        at_submission=[];self.e.submit=lambda *args:at_submission.append(self.e.config()['armed'])
        ns['DeskEngine'].enter(self.e,dict(symbol='NIFTY',decision='READY',exchange='NFO',contract='TESTCE',lot=50,lots=1,entry=100.,stop=90.,target=120.,qty=50,tick=.05))
        self.assertEqual(at_submission,[True]);self.assertFalse(self.e.config()['armed'])
    def test_actual_initialization_keeps_stock_and_index_risk_separate(self):
        stock=ns['StockDeskEngine'](FakeBroker(self.tmp.name+'/initialized_stock.db'))
        index=ns['IndexOptionsEngine'](FakeBroker(self.tmp.name+'/initialized_index.db'))
        stock_risk=ns['IndependentDeskRisk']('stock',stock);index_risk=ns['IndependentDeskRisk']('index',index)
        self.assertIsNot(stock_risk.lock,index_risk.lock)
        self.assertEqual(stock_risk.config()['stage1_activation'],5000.)
        self.assertEqual(stock_risk.config()['stage2_floor'],5000.)
        self.assertEqual(stock.config()['max_positions'],3)
        self.assertEqual(index.config()['mode'],'paper');self.assertFalse(index.config()['auto_start'])
        stock.put('shared-risk:live:'+clock.now.date().isoformat(),{'reason':'Desk daily loss limit reached','liquidate':True})
        self.assertIsNone(index.get('shared-risk:live:'+clock.now.date().isoformat()))
    def test_pending_fill_snapshot_race_defers_instead_of_false_permanent_halt(self):
        position(self.e);order(self.e)
        self.e.a.holdings=lambda:[dict(product='MIS',exchange='NFO',tradingsymbol='TESTCE',quantity=50)]
        self.e.verify_positions()
        self.assertTrue(self.e.position_verification_deferred);self.assertEqual(self.e.mismatches,set());self.assertIsNone(self.e.get('halt'))
        self.e.apply_order('o',dict(order_id='BROKER-1',status='CANCELLED',filled_quantity=50,average_price=100.))
        self.e.verify_positions();self.assertFalse(self.e.position_verification_deferred)
        self.assertEqual(self.e.mismatches,set());self.assertIsNone(self.e.get('halt'))
    def test_genuine_quantity_mismatch_without_pending_order_still_halts(self):
        position(self.e,qty=50,status='OPEN')
        self.e.a.holdings=lambda:[];self.e.verify_positions()
        self.assertEqual(self.e.mismatches,{'p'});self.assertIn('Broker/local position mismatch',self.e.get('halt'))

if __name__=='__main__':unittest.main(verbosity=2)
