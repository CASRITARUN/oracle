"""Stock strategy regressions. Synthetic paths/ledgers, no market-performance claim."""
import copy
import json
import math
import unittest
from datetime import timedelta
from unittest.mock import patch
import test_stock_execution as execution

runtime=execution.runtime
old=execution.old
ns=execution.ns
Runtime=runtime.Runtime

class LogicTests(unittest.TestCase):
    def setUp(self):
        execution.ExecutionTests.setUp(self)
        old.clock.now=old.clock.now.replace(hour=14,minute=0,second=0,microsecond=0)
    def tearDown(self):execution.ExecutionTests.tearDown(self)
    def path(self,n=5):
        start=old.clock.now.replace(hour=11)
        return [dict(ts=(start+timedelta(minutes=5*i)).isoformat(),close=100.,high=100.1,low=99.9) for i in range(n)]
    def test_complete_timeout_is_included_as_no_target(self):
        rows=self.path()
        self.assertIsNone(ns['StockDeskEngine']._historical_barrier_outcome(rows,0,1,1.,2.,4))
        self.assertEqual(Runtime._historical_barrier_outcome(rows,0,1,1.,2.,4),0)
    def test_overnight_jump_cannot_be_an_intraday_training_win(self):
        rows=self.path();rows[-1].update(ts=(old.clock.now+timedelta(days=1)).isoformat(),high=103.,close=103.)
        self.assertEqual(ns['StockDeskEngine']._historical_barrier_outcome(rows,0,1,1.,2.,4),1)
        self.assertIsNone(Runtime._historical_barrier_outcome(rows,0,1,1.,2.,4))
    def test_missing_or_unfinished_path_is_unknown(self):
        rows=self.path();rows[2]['ts']=rows[3]['ts']
        self.assertIsNone(Runtime._historical_barrier_outcome(rows,0,1,1.,2.,4))
        self.assertIsNone(Runtime._historical_barrier_outcome(self.path(3),0,1,1.,2.,4))
        rows=self.path();old.clock.now=old.clock.now.replace(hour=11,minute=22)
        self.assertIsNone(Runtime._historical_barrier_outcome(rows,0,1,1.,2.,4))
    def test_target_stop_and_ambiguous_bar_are_labelled_conservatively(self):
        for direction in (1,-1):
            rows=self.path();rows[2].update(high=103. if direction>0 else 100.1,low=97. if direction<0 else 99.9)
            self.assertEqual(Runtime._historical_barrier_outcome(rows,0,direction,1.,2.,4),1)
            rows[2].update(high=103.,low=97.)
            self.assertEqual(Runtime._historical_barrier_outcome(rows,0,direction,1.,2.,4),0)
    def test_old_historical_model_is_retired_without_entry_halt(self):
        m=dict(valid=True,status='VALIDATED',updated=old.clock.now.isoformat(),w=[1.])
        self.e.put('historical_setup_success_model',m)
        self.assertFalse(self.e.historical_success_model(True)['valid'])
        self.assertEqual(self.e.historical_success_model()['status'],'REBUILD REQUIRED')
        self.assertIsNone(self.e.get('halt'))
        m['stock_label_policy']=self.e._historical_policy();self.e.put('historical_setup_success_model',m)
        self.assertTrue(self.e.historical_success_model(True)['valid'])
        cfg=self.e.config();cfg['max_hold']=45;self.e.put('config',cfg)
        self.assertFalse(self.e.historical_success_model()['valid'])
    def test_dashboard_does_not_claim_old_models_are_valid(self):
        self.e.put('historical_setup_success_model',dict(valid=True,status='VALIDATED'))
        self.e.put('trade_success_model',dict(valid=True,quality_schema=2))
        self.e.model=lambda:self.fail('display must not perform a second model query')
        state=self.e.state()
        self.assertFalse(state['historical_success_model']['valid']);self.assertFalse(state['success_model']['valid'])
    def test_historical_proxy_cannot_overrule_large_direction_advantage(self):
        base=dict(self.seed,estimated_costs=100.,success_model_valid=True,historical_success_model_valid=True)
        weak=dict(base,confidence=.56,success_probability=.95,historical_setup_probability=.95,expected_r=1.)
        strong=dict(base,confidence=.72,success_probability=.51,historical_setup_probability=.51,expected_r=.1)
        self.assertGreater(ns['StockDeskEngine'].rank_signal(weak,3),ns['StockDeskEngine'].rank_signal(strong,3))
        self.assertGreater(Runtime.rank_signal(strong,3),Runtime.rank_signal(weak,3))
    def test_negative_net_target_fails_even_without_option_model(self):
        s=dict(self.seed,entry=100.,stop=90.,target=101.,qty=50,estimated_costs=80.)
        self.assertFalse(self.e.entry_quality_gate(s));self.assertIn('target does not cover',s['reason'])
    def test_costs_are_deducted_in_trade_scenarios_and_snapshot_is_frozen(self):
        s=dict(self.seed,entry=100.,stop=90.,target=120.,qty=100,estimated_costs=100.,features=[0.]*15)
        self.assertTrue(self.e.entry_quality_gate(s));self.assertEqual(s['trade_economics']['net_at_target'],1900.)
        self.assertEqual(s['trade_economics']['loss_at_stop'],1100.)
        frozen=list(s['option_learning_snapshot']['features']);s['confidence']=.1;s['entry']=90.
        self.assertEqual(s['option_learning_snapshot']['features'],frozen)
    def test_daily_remaining_loss_never_resizes_stock_plan(self):
        a=self.e.adaptive_entry_plan(self.seed,self.a.q,50,.05,None,100.)
        b=self.e.adaptive_entry_plan(self.seed,self.a.q,50,.05,None,50000.)
        for key in ('entry','stop','target','qty'):self.assertEqual(a[key],b[key])
        self.assertIsNone(a['risk_plan']['risk_budget'])
    def open_position(self,**kw):
        p=old.position(self.e,qty=100,status='OPEN',mode='paper')
        setup=dict(direction='UP',tick=.05,risk_plan=dict(policy='adaptive-v1',initial_stop=90.,strength=1.,confidence=.8))
        p.update(symbol='TEST',entry=100.,stop=90.,pnl=0.,fees=0.,setup=json.dumps(setup),**kw)
        with self.e.db() as c:c.execute('UPDATE desk_positions SET symbol=?,stop=?,setup=?,pnl=?,fees=? WHERE id=?',(p['symbol'],p['stop'],p['setup'],p['pnl'],p['fees'],p['id']))
        return p
    def test_single_or_repeated_weak_candle_does_not_halve_stop(self):
        p=self.open_position();ts=old.clock.now-timedelta(minutes=10)
        self.e.signals={'TEST':dict(direction='DOWN',confidence=.8,bar_ts=ts.isoformat())}
        for _ in range(4):self.e.adaptive_position_update(p,{'bid':94.},self.e.config())
        self.assertEqual(p['stop'],90.)
        self.e.signals['TEST']['bar_ts']=(ts+timedelta(minutes=5)).isoformat()
        self.e.adaptive_position_update(p,{'bid':94.},self.e.config());self.assertEqual(p['stop'],95.)
    def test_signal_recovery_resets_weakness_confirmation(self):
        p=self.open_position();ts=old.clock.now-timedelta(minutes=10)
        self.e.signals={'TEST':dict(direction='DOWN',confidence=.8,bar_ts=ts.isoformat())}
        self.e.adaptive_position_update(p,{'bid':94.},self.e.config())
        self.e.signals['TEST'].update(direction='UP')
        self.e.adaptive_position_update(p,{'bid':94.},self.e.config())
        self.e.signals['TEST'].update(direction='DOWN',bar_ts=(ts+timedelta(minutes=5)).isoformat())
        self.e.adaptive_position_update(p,{'bid':94.},self.e.config());self.assertEqual(p['stop'],90.)
    def test_partial_profit_is_not_charged_again_to_remaining_units(self):
        p=self.open_position();p.update(stop=100.,fees=100.,pnl=500.)
        self.e.signals={};self.e.exit_reserve=lambda *args:100.
        self.e.adaptive_position_update(p,{'bid':110.},self.e.config())
        self.assertEqual(p['stop'],100.)
    def test_cost_breakeven_rounds_up_and_cannot_widen_existing_stop(self):
        p=self.open_position();p.update(fees=29.9);self.e.signals={};self.e.exit_reserve=lambda *args:100.
        self.e.adaptive_position_update(p,{'bid':111.},self.e.config());self.assertEqual(p['stop'],101.3)
        self.e.adaptive_position_update(p,{'bid':95.},self.e.config());self.assertEqual(p['stop'],101.3)
    def samples(self,n=160):
        start=(old.clock.now-timedelta(days=40)).replace(hour=10)
        rows=[]
        for i in range(n):
            t=start+timedelta(days=i//4,minutes=(i%4)*10)
            rows.append(dict(id=str(i),start=t,end=t+timedelta(minutes=30),x=[float(i%2)]*20,y=i%2,gross_r=2. if i%2 else -1.))
        return rows
    def test_trade_validation_purges_actual_overlapping_holding_periods(self):
        rows=self.samples();cut1=rows[96]['start'];cut2=rows[128]['start']
        rows[95]['end']=cut2+timedelta(minutes=10)
        train,cal,val=Runtime._split_option_samples(rows)
        self.assertNotIn('95',[r['id'] for r in train])
        self.assertTrue(all(r['end']+timedelta(minutes=5)<min(v['start'] for v in cal) for r in train))
        self.assertTrue(all(r['end']+timedelta(minutes=5)<min(v['start'] for v in val) for r in cal))
    def test_real_option_training_uses_frozen_entry_features_and_disjoint_windows(self):
        for sample in self.samples():
            setup=dict(decision='READY',confidence=.99,features=[99.]*15,option_learning_snapshot=dict(schema=1,features=sample['x'],planned_risk=100.,recorded_at=sample['start'].isoformat()))
            with self.e.db() as c:c.execute('INSERT INTO desk_positions(id,ts,exit_ts,symbol,mode,status,pnl,fees,setup) VALUES(?,?,?,?,?,?,?,?,?)',
                (sample['id'],sample['start'].isoformat(),sample['end'].isoformat(),'TEST','paper','CLOSED',200. if sample['y'] else -100.,10.,json.dumps(setup)))
        m=self.e.train_success_model(True)
        self.assertEqual(m['quality_schema'],3);self.assertEqual(m['samples'],160);self.assertEqual(m['excluded_samples'],0)
        self.assertLess(m['train_end'],m['calibration_start']);self.assertLess(m['calibration_end'],m['validation_start'])
        self.assertEqual(len(m['mu']),20);self.assertLess(max(m['mu']),1.)
        self.assertTrue(m['valid'],m);self.assertLess(m['brier'],m['baseline_brier'])
    def test_old_trade_features_are_excluded_without_fabricated_reconstruction(self):
        with self.e.db() as c:c.execute('INSERT INTO desk_positions(id,ts,exit_ts,mode,status,pnl,fees,setup) VALUES(?,?,?,?,?,?,?,?)',('old',(old.clock.now-timedelta(hours=1)).isoformat(),old.clock.now.isoformat(),'paper','CLOSED',100.,10.,json.dumps(self.seed)))
        m=self.e.train_success_model(True);self.assertEqual(m['samples'],0);self.assertEqual(m['excluded_samples'],1);self.assertFalse(m['valid'])
    def test_live_outcome_learning_never_uses_paper_trades(self):
        start=old.clock.now-timedelta(hours=1);end=old.clock.now-timedelta(minutes=1)
        setup=dict(option_learning_snapshot=dict(schema=1,features=[0.]*20,planned_risk=100.,recorded_at=start.isoformat()))
        for mode,pnl in [('paper',200.),('live',-100.)]:
            with self.e.db() as c:c.execute('INSERT INTO desk_positions(id,ts,exit_ts,mode,status,pnl,fees,setup) VALUES(?,?,?,?,?,?,?,?)',
                (mode,start.isoformat(),end.isoformat(),mode,'CLOSED',pnl,10.,json.dumps(setup)))
        paper=self.e.train_success_model(True)
        cfg=self.e.config();cfg['mode']='live';self.e.put('config',cfg)
        live=self.e.train_success_model(True)
        self.assertEqual(paper['samples'],1);self.assertEqual(live['samples'],1)
        self.assertEqual(paper['mode'],'paper');self.assertEqual(live['mode'],'live')
        self.assertEqual(self.e.success_model()['mode'],'live');self.assertEqual(self.e.state()['success_model']['mode'],'live')
        self.assertEqual(self.e.get('trade_success_model')['mode'],'paper')
        self.assertEqual(self.a.calls,[])
    def test_auc_and_brier_levels_do_not_become_entry_cutoffs(self):
        m=dict(self.m,auc=.49,brier=.30,ts=old.clock.now.isoformat())
        with self.e.db() as c:c.execute('UPDATE desk_models SET payload=?',(json.dumps(m),))
        self.e.runtime_cache.clear()
        self.assertTrue(self.e.model_quality()['live_ok'])
        s=dict(self.seed);self.assertTrue(self.e.entry_quality_gate(s))
    def test_paper_and_live_results_and_cost_losses_are_separate(self):
        start=old.clock.now-timedelta(hours=1);end=old.clock.now-timedelta(minutes=1)
        base=dict(ts=start.isoformat(),exit_ts=end.isoformat(),setup='{}',exit_reason='Target reached')
        rows=[dict(base,mode='paper',pnl=200.,fees=20.),dict(base,mode='live',pnl=50.,fees=100.),dict(base,mode='live',pnl=None,fees=10.)]
        report=Runtime.trade_outcome_review(rows)['modes']
        self.assertEqual(report['paper']['net'],180.);self.assertEqual(report['live']['net'],-50.)
        self.assertEqual(report['live']['cost_turned_losses'],1);self.assertEqual(report['live']['excluded'],1)
    def test_missing_trade_values_are_not_reported_as_flat_results(self):
        report=Runtime.trade_outcome_review([dict(mode='live',pnl=None,fees=None)])['modes']['live']
        self.assertEqual(report['trades'],0);self.assertEqual(report['excluded'],1);self.assertIsNone(report['average_net']);self.assertIsNone(report['net'])

if __name__=='__main__':unittest.main()
