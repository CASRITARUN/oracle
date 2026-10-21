"""Recorded execution, policy-aware learning and conservative replay. Offline only."""
import copy
import json
import math
import threading
import unittest
from datetime import timedelta
from unittest.mock import patch
import test_stock_logic as logic

old=logic.old;ns=logic.ns;Runtime=logic.Runtime

class EvidenceTests(unittest.TestCase):
    def setUp(self):logic.execution.ExecutionTests.setUp(self)
    def tearDown(self):logic.execution.ExecutionTests.tearDown(self)
    def fill(self):
        self.e.execute_candidates();return self.e.active()[0]
    def test_paper_trace_has_confirmed_fill_and_no_fabricated_broker_ack(self):
        p=self.fill();self.e._flush_evidence()
        traces=self.e.rows('SELECT payload FROM stock_execution_trace WHERE position_id=?',(p['id'],))
        self.assertEqual(len(traces),1);trace=json.loads(traces[0]['payload'])
        stages=[r['stage'] for r in trace['events']]
        for stage in ('QUALIFIED','CHECKING','CHECKS_PASSED','SUBMITTING','FILLED'):self.assertIn(stage,stages)
        self.assertNotIn('ACKNOWLEDGED',stages);self.assertEqual(self.a.calls,[])
        fill=next(json.loads(r['payload']) for r in self.e.rows("SELECT payload FROM desk_trade_events WHERE kind='FILL'"))
        self.assertIsNotNone(fill['execution_measurement']['slippage_vs_ask_bps'])
        self.assertEqual(fill['execution_measurement']['arrival_quote']['ask'],self.a.q['ask'])
    def test_wait_is_explicit_without_inventing_an_order(self):
        self.a.quote_batch=lambda *a,**k:{'TESTCE':(None,'Quote timeout')}
        self.e.execute_candidates();self.e._flush_evidence()
        self.assertEqual(self.e.rows('SELECT * FROM desk_orders'),[])
        traces=self.e.state()['execution_traces'];self.assertTrue(any(t['stage']=='WAIT' and 'Quote timeout' in t['reason'] for t in traces))
    def test_trace_recording_never_calls_config_or_broker(self):
        seed=dict(self.seed,execution_mode='paper');self.e.config=lambda:self.fail('Trace called configuration DB')
        self.assertIsNotNone(self.e._entry_trace(seed,'CHECKING','read only'));self.assertEqual(self.a.calls,[])
    def test_trace_dedup_and_queue_are_bounded(self):
        seed=dict(self.seed,execution_mode='paper')
        for _ in range(30):identity=self.e._entry_trace(seed,'WAIT','same reason')
        self.assertLess(len(self.e.entry_traces[identity]['events']),5)
        for i in range(300):self.e._entry_trace(dict(seed,bar_ts=str(i)),'QUALIFIED')
        self.assertLessEqual(len(self.e.entry_traces),256);self.assertLessEqual(len(self.e.trace_dirty),256)
        self.assertGreater(self.e.evidence_status['dropped_traces'],0)
    def test_evidence_write_failure_does_not_drop_pending_measurements(self):
        self.e._entry_trace(dict(self.seed,execution_mode='paper'),'WAIT','sample')
        with patch.object(self.e,'db',side_effect=RuntimeError('disk unavailable')):self.e._flush_evidence()
        self.assertTrue(self.e.trace_dirty);self.assertEqual(self.e.evidence_status['status'],'RETRY')
        self.e._flush_evidence();self.assertFalse(self.e.trace_dirty)
    def test_measurements_include_realized_partial_profit_and_exit_fees(self):
        p=self.fill();p.update(entry=100.,qty=50,pnl=500.,fees=80.)
        q=dict(self.a.q,bid=105.,ask=105.1,quote_ts=old.clock.now.isoformat())
        self.e.record_observation(p,q,decision='HOLD')
        m=self.e.trade_marks[p['id']];reserve=self.e.order_charge('SELL',5250.,brokerage=25.)
        self.assertAlmostEqual(m['last_net'],round(500-80+250-reserve,2));self.assertEqual(m['samples'],1)
        self.e.record_observation(p,q,decision='HOLD');self.assertEqual(m['samples'],1)
        old.clock.now+=timedelta(seconds=40);q['quote_ts']=old.clock.now.isoformat();self.e.record_observation(p,q,decision='HOLD')
        self.assertTrue(m['data_gap']);self.assertEqual(m['max_gap_seconds'],40.)
    def test_stale_quote_does_not_become_a_profit_extreme(self):
        p=self.fill();q=dict(self.a.q,bid=200.,quote_ts=(old.clock.now-timedelta(seconds=80)).isoformat())
        self.e.record_observation(p,q,decision='HOLD');self.assertNotIn(p['id'],self.e.trade_marks)
    def test_horizon_sample_requires_observed_quote_near_due_time(self):
        p=self.fill();old.clock.now+=timedelta(minutes=15)
        self.e.record_observation(p,dict(self.a.q,quote_ts=old.clock.now.isoformat()),decision='HOLD')
        self.assertIn('15',self.e.trade_marks[p['id']]['horizons'])
        old.clock.now+=timedelta(minutes=16)
        self.e.record_observation(p,dict(self.a.q,quote_ts=old.clock.now.isoformat()),decision='HOLD')
        self.assertNotIn('30',self.e.trade_marks[p['id']]['horizons'])
    def test_policy_features_are_frozen_and_legacy_policy_unknown_is_excluded(self):
        s=dict(self.seed);self.e.entry_quality_gate(s);snapshot=copy.deepcopy(s['option_learning_snapshot'])
        cfg=self.e.config();cfg['max_hold']=30;self.e.put('config',cfg)
        self.assertEqual(len(snapshot['policy_features']),22);self.assertEqual(snapshot['policy_features'][-2],.25)
        self.assertEqual(s['option_learning_snapshot'],snapshot)
        row=dict(id='new',ts=snapshot['recorded_at'],exit_ts=(old.clock.now+timedelta(minutes=1)).isoformat(),pnl=100.,fees=10.,setup=json.dumps(s))
        old.clock.now+=timedelta(minutes=2);self.assertEqual(len(Runtime.option_samples([row])),1)
        del s['option_learning_snapshot']['feature_schema'];row['setup']=json.dumps(s)
        self.assertEqual(Runtime.option_samples([row]),[])
    def test_evidence_sizing_reduces_lots_without_compressing_stop(self):
        self.a.q['ask_qty']=100000;self.a.q['ask_levels']=[{'price':self.a.q['ask'],'quantity':100000}]
        seed=dict(self.seed,confidence=.7)
        a=self.e.adaptive_entry_plan(seed,self.a.q,50,.05,None,100.)
        self.e.learning_cache={'sizing_review':{'paper':{'7':dict(trades=50,sessions=12,upper_mean_session_r=-.1,updated=old.clock.now.isoformat())}}}
        b=self.e.adaptive_entry_plan(seed,self.a.q,50,.05,None,99999.)
        self.assertGreater(a['qty'],50);self.assertLess(b['qty'],a['qty']);self.assertEqual(b['qty']%50,0)
        self.assertGreaterEqual(b['qty'],a['qty']*.75)
        self.assertEqual((a['stop'],a['target']),(b['stop'],b['target']));self.assertIsNone(b['risk_plan']['risk_budget'])
    def test_insufficient_stale_and_other_mode_sizing_evidence_is_ignored(self):
        base=self.e.adaptive_entry_plan(self.seed,self.a.q,50,.05,None,None)
        for support in [dict(trades=39,sessions=10,upper_mean_session_r=-1.,updated=old.clock.now.isoformat()),dict(trades=50,sessions=10,upper_mean_session_r=-1.,updated=(old.clock.now-timedelta(days=3)).isoformat())]:
            self.e.learning_cache={'sizing_review':{'paper':{'7':support}}}
            sized=self.e.adaptive_entry_plan(self.seed,self.a.q,50,.05,None,None);self.assertEqual(sized['qty'],base['qty'])
    def model(self,weight=4.,identity='candidate'):
        return dict(id=identity,mode='paper',valid=True,quality_schema=4,w=[weight]+[0.]*21,bias=0.,mu=[0.]*22,sd=[1.]*22,
            train_win_rate=.5,validation_samples=120,calibration={'enabled':False},payoff={'win_gross_r':2.,'loss_gross_r':-1.},
            updated=(old.clock.now-timedelta(days=15)).isoformat(),validation_end=(old.clock.now-timedelta(days=15)).isoformat())
    def outcomes(self,n=120):
        start=old.clock.now-timedelta(days=12);out=[]
        for i in range(n):
            when=start+timedelta(days=i//10,minutes=i%10*3);win=i%2
            out.append(dict(id=str(i),start=when,end=when+timedelta(minutes=2),x=[1. if win else -1.]+[0.]*21,
                y=win,gross_r=2. if win else -1.,net_r=1.9 if win else -1.1,cost_r=.1,confidence=.7))
        return out
    def test_promotion_requires_later_net_evidence_and_three_time_windows(self):
        m=self.model();old_m=self.model(0.,'champion');d=Runtime.option_promotion_decision(m,old_m,self.outcomes())
        self.assertTrue(d['promoted'],d);self.assertEqual(len(d['challenger_report']['windows']),3)
        self.assertLess(d['challenger_report']['brier'],d['champion_report']['brier'])
    def test_more_accurate_candidate_with_losing_net_outcomes_is_rejected(self):
        data=self.outcomes()
        for r in data:r['net_r']=-.1
        self.assertFalse(Runtime.option_promotion_decision(self.model(),None,data)['promoted'])
    def test_champion_prior_holdout_cannot_be_reused(self):
        m=self.model();prior=self.model(0.);prior['validation_end']=old.clock.now.isoformat()
        d=Runtime.option_promotion_decision(m,prior,self.outcomes());self.assertFalse(d['promoted']);self.assertIn('last validation',d['reason'])
    def test_one_session_cannot_justify_model_promotion(self):
        data=self.outcomes()
        for i,r in enumerate(data):r.update(start=old.clock.now-timedelta(minutes=120-i),end=old.clock.now-timedelta(minutes=119-i))
        self.assertFalse(Runtime.option_promotion_decision(self.model(),None,data)['promoted'])
    def test_learning_candidate_does_not_overwrite_eligible_champion(self):
        m=self.model();self.e.put('trade_success_model',m);self.e.success_model_cache=None
        self.e.train_success_model(True)
        self.assertEqual(self.e.get('trade_success_model')['id'],m['id']);self.assertEqual(self.e.get('stock_option_review:paper')['samples'],0)
    def test_contract_bonus_requires_eligible_option_evidence_and_is_bounded(self):
        s=dict(option_expected_r=100.,option_rank_weight=500.,live_success_model_valid=False)
        self.assertEqual(Runtime.option_contract_bonus(s),0.)
        s['live_success_model_valid']=True;self.assertLessEqual(Runtime.option_contract_bonus(s),10.)
    def test_rollback_does_not_arm_or_touch_index_risk(self):
        cfg=self.e.config();cfg['armed']=False;self.e.put('config',cfg)
        previous=self.model(identity='previous');previous['updated']=old.clock.now.isoformat()
        self.e.put('stock_option_previous:paper',previous)
        result=self.e.control('rollback-option-model',{});self.assertEqual(result['model_id'],'previous')
        self.assertFalse(self.e.config()['armed']);self.assertEqual(self.a.calls,[])
    def test_rollback_rejects_armed_active_stale_or_wrong_mode(self):
        with self.assertRaises(ValueError):self.e.rollback_option_model('paper')
        cfg=self.e.config();cfg['armed']=False;self.e.put('config',cfg)
        self.e.put('stock_option_previous:paper',self.model())
        with self.assertRaises(ValueError):self.e.rollback_option_model('paper')
        with self.assertRaises(ValueError):self.e.rollback_option_model('live')
    def test_rollback_preserves_latest_evaluated_boundary(self):
        cfg=self.e.config();cfg['armed']=False;self.e.put('config',cfg)
        previous=self.model(identity='previous');previous['updated']=old.clock.now.isoformat()
        current=self.model(identity='current');current['validation_end']=(old.clock.now-timedelta(days=1)).isoformat()
        boundary=old.clock.now.isoformat()
        self.e.put('stock_option_previous:paper',previous);self.e.put('trade_success_model',current)
        self.e.put('stock_option_review:paper',dict(evaluated_through=boundary))
        self.e.rollback_option_model('paper')
        self.assertEqual(self.e.get('stock_option_review:paper')['evaluated_through'],boundary)
    def replay(self,bids,gap=False):
        first=old.clock.now-timedelta(seconds=5*len(bids));policy=dict(schema=1,initial_stop=90.,fee_per_order=25.,slippage_bps=5.)
        p=dict(id='r',mode='paper',pnl=50.,fees=60.,setup=json.dumps(dict(entry_policy=policy,tick=.05)),exit_ts=old.clock.now.isoformat())
        events=[dict(ts=first.isoformat(),kind='FILL',payload=json.dumps(dict(side='BUY',fill_delta=100)))]
        for i,bid in enumerate(bids):
            ts=(first+timedelta(seconds=i*5)).isoformat();events.append(dict(ts=ts,kind='OBSERVATION',payload=json.dumps(dict(quote=dict(quote_ts=ts,bid=bid,bid_qty=1000)))))
        if gap:events.append(dict(ts=first.isoformat(),kind='DATA_GAP',payload='{}'))
        return self.e.replay_exit_candidates(p,[dict(side='BUY',filled=100,notional=10000.)],events)
    def test_exit_replay_uses_observed_crossing_not_optimistic_stop_price(self):
        report=self.replay([100.,111.,96.]);trial=report['policies']['NET_BREAKEVEN_1R']
        self.assertEqual(trial['status'],'TRIGGERED');self.assertLess(trial['price'],96.);self.assertLess(trial['net'],0)
    def test_censored_path_has_no_fabricated_pnl(self):
        report=self.replay([100.,101.,102.])
        for trial in report['policies'].values():self.assertEqual(trial['status'],'CENSORED');self.assertNotIn('net',trial)
    def test_quote_gap_excludes_exit_replay(self):
        report=self.replay([100.,111.,96.],True)
        for trial in report['policies'].values():self.assertEqual(trial['status'],'INCOMPLETE')
    def test_display_contains_mode_specific_trace_without_trading_dependencies(self):
        self.e._entry_trace(dict(self.seed,execution_mode='live'),'UNKNOWN','Broker outcome unknown')
        self.e._flush_evidence();self.e.block=lambda:self.fail('Display called trading gate')
        state=self.e.state();self.assertTrue(all(t['mode']=='paper' for t in state['execution_traces']))
        json.dumps(state,allow_nan=False)
    def test_malformed_optional_trace_does_not_break_display(self):
        with self.e.db() as c:
            c.execute('INSERT INTO stock_execution_trace VALUES(?,?,?,?,?,?)',('bad','paper','TEST',None,old.clock.now.isoformat(),'{"events": []}'))
        state=self.e.state();self.assertTrue(any('Execution trace' in w for w in state['display_warnings']))
        self.assertTrue(all(t.get('id')!='bad' for t in state['execution_traces']))
    def test_research_survives_incomplete_legacy_financial_records(self):
        row=dict(id='legacy',mode='paper',ts=old.clock.now.isoformat(),exit_ts=None,setup='{}',pnl=None,fees=None)
        report,sizing=self.e._review_trade_evidence([row])
        self.assertIsNone(report['modes']['paper']['mean_giveback']);self.assertEqual(sizing['paper']['7']['trades'],0)
    def test_malformed_optional_research_is_excluded(self):
        row=dict(id='legacy',mode='paper',ts=old.clock.now.isoformat(),exit_ts=None,setup='{}',pnl=None,fees=None)
        self.e.put('stock_exit_replay:legacy',{'policies':['bad']})
        report,_=self.e._review_trade_evidence([row]);self.assertEqual(report['modes']['paper']['excluded_records'],1)
    def test_journal_includes_saved_trace_and_quote_path_metrics(self):
        p=self.fill();self.e.record_observation(p,self.a.q,decision='HOLD');self.e._flush_evidence()
        review=self.e.trade_review(p['id'])
        self.assertEqual(review['execution_trace'][0]['position_id'],p['id'])
        self.assertEqual(review['trade_metrics']['samples'],1);self.assertIn('exit_research',review)
    def test_rollback_cannot_race_model_promotion(self):
        self.e.success_lock.acquire()
        try:
            with self.assertRaisesRegex(ValueError,'review is running'):self.e.rollback_option_model('paper')
        finally:self.e.success_lock.release()

if __name__=='__main__':unittest.main()
