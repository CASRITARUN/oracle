"""Regressions for populated/legacy Stock display reads; no broker I/O."""
import json
import sqlite3
import threading
import time
import unittest
from datetime import datetime, date
from types import SimpleNamespace
import numpy as np
import test_stock_runtime as runtime

class DisplayTests(unittest.TestCase):
    def setUp(self):
        self.fixture=runtime.RuntimeTests();self.fixture.setUp();self.e=self.fixture.e
    def tearDown(self):self.fixture.tearDown()
    def finish(self,box):
        self.assertTrue(box.done.wait(2),'display job did not finish')
    def test_missing_historical_exit_does_not_hide_current_orders(self):
        runtime.old.position(self.e,qty=0,status='CLOSED',mode='paper')
        runtime.old.order(self.e,status='UNKNOWN',filled=0)
        s=self.e.state()
        self.assertEqual(s['orders'][0]['status'],'UNKNOWN');self.assertEqual(s['pending_orders'][0]['filled'],0)
        self.assertEqual(s['evidence']['net'],0.)
        self.assertIsNone(s['evidence']['checks'][1]['value']);self.assertFalse(s['evidence']['ready'])
        self.assertIn('Paper history',' '.join(s['display_warnings']))
        self.assertIsNone(self.e.rows('SELECT exit_ts FROM desk_positions')[0]['exit_ts'])
        self.assertEqual(self.e.a.calls,[])
    def test_missing_financial_values_are_unknown_not_zero(self):
        runtime.old.position(self.e,qty=0,status='CLOSED',mode='paper')
        with self.e.db() as c:c.execute('UPDATE desk_positions SET fees=NULL,risk=NULL')
        s=self.e.state()
        self.assertIsNone(s['evidence']['net']);self.assertIsNone(s['evidence']['win_rate'])
        self.assertIsNone(s['trades'][0]['fees']);self.assertEqual(s['evidence']['equity'],[])
        self.assertFalse(s['evidence']['ready'])
    def test_missing_old_fill_measurements_preserve_active_position(self):
        p=runtime.old.position(self.e,qty=100,status='OPEN',mode='live');runtime.old.order(self.e,status='COMPLETE',filled=100)
        self.e.put('execution_fill:o',dict(order_id='o',mode='live',filled=100,delay_seconds=None))
        self.e.health[p['id']]=dict(bid=np.float64(101.),observed_mono=time.monotonic())
        cfg=self.e.config();cfg['mode']='live';self.e.put('config',cfg)
        s=self.e.state()
        self.assertEqual(s['positions'][0]['qty'],100);self.assertEqual(s['positions'][0]['unrealized'],100.)
        self.assertIsNone(s['execution_quality']['mean_confirmation_seconds']);self.assertIsNone(s['execution_quality']['mean_slippage_vs_limit_bps'])
        self.assertEqual(s['execution_quality']['samples'],1);self.assertEqual(s['execution_quality']['timing_samples'],0)
    def test_optional_bad_model_metadata_does_not_erase_valid_ledger(self):
        runtime.old.position(self.e,qty=100,status='OPEN',mode='paper')
        with self.e.db() as c:c.execute("INSERT OR REPLACE INTO desk_meta VALUES('trade_success_model','{bad-json')")
        self.e.put('historical_setup_success_model',['old-shape'])
        s=self.e.state();self.assertEqual(s['positions'][0]['qty'],100)
        self.assertEqual(s['model']['id'],'champ');self.assertIsNone(s['success_model']);self.assertIsNone(s['historical_success_model'])
        self.assertEqual(len(s['display_warnings']),2)
    def test_invalid_config_keeps_last_good_snapshot_but_marks_it_stale(self):
        box=runtime.Read();box.response(self.e);self.finish(box);self.assertIsNotNone(box.payload)
        with self.e.db() as c:c.execute("UPDATE desk_meta SET value='null' WHERE key='config'")
        box.invalidate()
        with self.assertLogs('stock-tests',level='ERROR'):
            box.response(self.e);self.finish(box)
        s=box.response(self.e)
        self.assertTrue(s['dashboard']['stale']);self.assertEqual(s['dashboard']['status'],'STALE')
        self.assertEqual(s['dashboard']['failure']['phase'],'saved configuration')
        self.assertEqual(s['dashboard']['failure']['code'],'DISPLAY_DATA')
        self.assertEqual(s['model']['id'],'champ')
    def test_numpy_scalars_arrays_and_dates_encode_with_correct_json_types(self):
        stamp=datetime(2026,10,15,10,0)
        e=SimpleNamespace(state=lambda:dict(signals=[dict(count=np.int64(3),qualified=np.bool_(True),prob=np.float32(.75),
            ts=stamp,expiry=date(2026,10,29),curve=np.array([1.,np.nan]))]),config_defaults={})
        box=runtime.Read();box.response(e);self.finish(box);s=box.response(e)
        self.assertFalse(s['dashboard']['stale']);signal=s['signals'][0]
        self.assertIs(type(signal['count']),int);self.assertIs(signal['qualified'],True);self.assertEqual(signal['prob'],.75)
        self.assertEqual(signal['ts'],stamp.isoformat());self.assertEqual(signal['expiry'],'2026-10-29');self.assertEqual(signal['curve'],[1.,None])
        json.dumps(s,allow_nan=False)
    def test_partial_statistics_have_current_ledger_not_false_connection_failure(self):
        runtime.old.position(self.e,qty=0,status='CLOSED',mode='paper')
        box=runtime.Read();box.response(self.e);self.finish(box);s=box.response(self.e)
        self.assertEqual(s['dashboard']['status'],'PARTIAL');self.assertFalse(s['dashboard']['stale']);self.assertTrue(s['dashboard']['has_snapshot'])
        self.assertIsNone(s['dashboard']['error'])
    def test_transient_database_failure_recovers_without_new_orders_or_manual_reset(self):
        count=[]
        def state():
            count.append(1)
            if len(count)==1:raise sqlite3.OperationalError('database is locked')
            return dict(orders=[dict(status='UNKNOWN',filled=0)])
        e=SimpleNamespace(state=state,config_defaults={},display_read_phase='position and order ledger');box=runtime.Read()
        with self.assertLogs('stock-tests',level='ERROR'):
            box.response(e);self.finish(box)
        s=box.response(e);self.assertEqual(s['dashboard']['status'],'ERROR');self.assertEqual(s['dashboard']['failure']['code'],'DATABASE_BUSY')
        self.assertLessEqual(s['dashboard']['retry_after_seconds'],.5);self.assertEqual(len(count),1)
        box.retry_at=0;box.response(e);self.finish(box);s=box.response(e)
        self.assertFalse(s['dashboard']['stale']);self.assertIsNone(s['dashboard']['error']);self.assertIsNone(s['dashboard']['failure'])
        self.assertEqual(s['orders'][0]['status'],'UNKNOWN');self.assertEqual(len(count),2)
    def test_fast_cold_read_can_return_data_in_first_http_poll(self):
        box=runtime.Read();e=SimpleNamespace(state=lambda:{'orders':[]},config_defaults={})
        s=box.response(e,wait_seconds=.1)
        self.assertEqual(s['dashboard']['status'],'CURRENT');self.assertTrue(s['dashboard']['has_snapshot'])
    def test_slow_cold_read_stays_bounded_and_single_flight(self):
        gate=threading.Event();calls=[]
        def state():calls.append(1);gate.wait(2);return {'orders':[]}
        box=runtime.Read();e=SimpleNamespace(state=state,config_defaults={});started=time.monotonic()
        try:
            s=box.response(e,wait_seconds=.1)
            self.assertLess(time.monotonic()-started,.3);self.assertEqual(s['dashboard']['status'],'LOADING')
            for _ in range(10):box.response(e)
            self.assertEqual(calls,[1])
        finally:gate.set();self.finish(box)
    def test_failure_diagnostic_excludes_raw_exception_contents(self):
        def state():raise TypeError('a private broker payload must not reach the page')
        box=runtime.Read();e=SimpleNamespace(state=state,config_defaults={},display_read_phase='paper history and fill statistics')
        with self.assertLogs('stock-tests',level='ERROR'):
            box.response(e);self.finish(box)
        s=box.response(e);encoded=json.dumps(s)
        self.assertNotIn('private broker payload',encoded);self.assertEqual(s['dashboard']['failure']['exception_type'],'TypeError')
        self.assertEqual(s['dashboard']['failure']['phase'],e.display_read_phase)

if __name__=='__main__':unittest.main()
