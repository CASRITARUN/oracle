"""Real SQLite contention and fake-broker recovery; no external requests."""
import ast
import concurrent.futures
import json
import logging
import os
from pathlib import Path
import secrets
import sqlite3
import tempfile
import threading
import time
from types import MethodType
import unittest
from unittest.mock import patch
import test_stock_execution as execution

runtime=execution.runtime
old=runtime.old
Adapter=runtime.Adapter

class ConnectionTests(unittest.TestCase):
    def setUp(self):
        execution.ExecutionTests.setUp(self)
        self.a.db=MethodType(Adapter.db,self.a)
        with self.a.db() as c:c.execute('PRAGMA journal_mode=WAL')
        self.e._display_risk_at=float('inf')
    def tearDown(self):runtime.RuntimeTests.tearDown(self)
    def recover(self):
        self.e._supervision_next_at=0;self.e._supervision_retry_at=0
        self.e.supervise()
    def writer(self):
        writer=sqlite3.connect(self.a.path)
        writer.execute('BEGIN IMMEDIATE')
        writer.execute("INSERT OR REPLACE INTO desk_meta VALUES('test_external_writer','true')")
        return writer
    def live_fixture(self,status='SUBMITTING'):
        cfg=self.e.config();cfg.update(mode='live',live_override=True);self.e.put('config',cfg)
        p=old.position(self.e)
        old.order(self.e,status=status)
        row=self.e.rows('SELECT * FROM desk_orders')[0]
        broker=dict(order_id='BROKER-1',tag=row['tag'],tradingsymbol=p['contract'],exchange=p['exchange'],
            transaction_type='BUY',quantity=row['qty'],product='MIS',status='COMPLETE',filled_quantity=row['qty'],average_price=100.)
        self.a.orders=lambda:[broker]
        self.a.holdings=lambda:[dict(product='MIS',exchange=p['exchange'],tradingsymbol=p['contract'],quantity=row['qty'])]
        self.e.manage=lambda **kw:None
        self.e.maintain_pending_limits=lambda:None
        return row,broker
    def test_actual_external_writer_recovers_then_fills_one_paper_order(self):
        writer=self.writer()
        try:
            started=time.monotonic();self.e.supervise()
            self.assertLess(time.monotonic()-started,3)
            self.assertTrue(self.e.database_recovery.is_set())
            self.assertEqual(self.e.database_status['status'],'RETRYING')
            self.assertIn('automatic',self.e.block())
            self.assertIsNone(self.e.get('halt'))
            self.assertTrue(self.e.config()['armed'])
            # A WAL reader still renders the last committed ledger while locked.
            state=self.e.state();self.assertEqual(state['database']['status'],'RETRYING')
            self.e.execute_candidates();self.assertEqual(self.e.rows('SELECT * FROM desk_orders'),[])
        finally:writer.rollback();writer.close()
        self.recover()
        self.assertFalse(self.e.database_recovery.is_set())
        self.assertEqual(self.e.database_status['status'],'OK')
        self.assertTrue(self.e.execution_event.is_set())
        self.e.execute_candidates();self.e.execute_candidates()
        orders=self.e.rows('SELECT * FROM desk_orders');self.assertEqual(len(orders),1,self.e.execution_status)
        self.assertEqual(orders[0]['status'],'COMPLETE');self.assertEqual(self.a.calls,[])
    def test_legacy_database_halt_clears_after_checks_without_paper_broker_read(self):
        self.a.holdings=lambda:self.fail('Paper-only recovery requested broker holdings')
        for message in ('database is locked','database table is locked','database schema is locked'):
            with self.subTest(message=message):
                self.e.put('halt','Position supervision error: '+message)
                self.recover();self.assertIsNone(self.e.get('halt'));self.assertTrue(self.e.config()['armed'])
    def test_position_stop_supervision_resumes_after_database_recovers(self):
        self.e.execute_candidates();position=self.e.active()[0]
        self.e._database_busy(sqlite3.OperationalError('database is locked'))
        self.a.q.update(bid=position['stop']-.1,ask=position['stop'],quote_ts=old.clock.now.isoformat())
        self.recover()
        closed=self.e.rows('SELECT * FROM desk_positions')[0]
        self.assertEqual(closed['status'],'CLOSED');self.assertEqual(closed['exit_reason'],'Stop reached')
        self.assertFalse(self.e.database_recovery.is_set());self.assertEqual(self.a.calls,[])
    def test_daily_manual_and_real_validation_halts_survive_recovery(self):
        for message in ('Daily loss limit reached','Manual halt','Position supervision error: Invalid broker fill'):
            self.e.put('halt',message);self.e._database_busy(sqlite3.OperationalError('database is locked'))
            self.recover();self.assertEqual(self.e.get('halt'),message)
            self.assertFalse(self.e.database_recovery.is_set())
        self.e.control('disarm',{});self.e.put('halt','Position supervision error: database is locked')
        self.recover();self.assertFalse(self.e.config()['armed']);self.assertTrue(self.e.operator_pause.is_set())
    def test_recovery_does_not_clear_concurrently_replaced_halt(self):
        self.e.put('halt','Position supervision error: database is locked')
        self.e.manage=lambda **kw:self.e.put('halt','Daily profit floor reached')
        self.recover();self.assertEqual(self.e.get('halt'),'Daily profit floor reached')
    def test_a_new_busy_event_during_recovery_is_not_lost(self):
        self.e._database_busy(sqlite3.OperationalError('database is locked'))
        self.e.manage=lambda **kw:self.e._database_busy(sqlite3.OperationalError('database is locked'))
        self.recover();self.assertTrue(self.e.database_recovery.is_set())
        self.e.manage=lambda **kw:None
        self.recover();self.assertFalse(self.e.database_recovery.is_set())
    def test_live_pending_and_quantity_mismatch_do_not_clear_legacy_halt(self):
        self.live_fixture()
        self.e.reconcile=lambda:None
        self.e.verify_positions=lambda:None
        message='Position supervision error: database is locked';self.e.put('halt',message)
        self.recover();self.assertEqual(self.e.get('halt'),message)
        with self.e.db() as c:c.execute("UPDATE desk_orders SET status='COMPLETE'")
        self.e.mismatches={'p'};self.recover();self.assertEqual(self.e.get('halt'),message)
    def test_db_failure_during_reconciliation_does_not_disarm_or_claim_broker_error(self):
        self.live_fixture()
        apply=self.e.apply_order
        self.e.apply_order=lambda *a,**kw:(_ for _ in ()).throw(sqlite3.OperationalError('database is locked'))
        self.recover()
        self.assertTrue(self.e.database_recovery.is_set());self.assertTrue(self.e.config()['armed'])
        self.assertIsNone(self.e.get('halt'))
        self.e.apply_order=apply;self.recover()
        self.assertFalse(self.e.database_recovery.is_set());self.assertTrue(self.e.config()['armed'])
        self.assertEqual(self.e.rows('SELECT status FROM desk_orders')[0]['status'],'COMPLETE')
        self.assertEqual(self.a.calls,[])
    def test_lock_after_broker_ack_recovers_by_tag_without_submitting_again(self):
        cfg=self.e.config();cfg.update(mode='live',live_override=True);self.e.put('config',cfg)
        p=old.position(self.e);writers=[];accepted=[]
        self.e.reprice_stock_entry=lambda position:(position,100,100.)
        def place(exchange,contract,side,qty,price,tag):
            accepted.append(dict(order_id='ACK-1',tag=tag,tradingsymbol=contract,exchange=exchange,transaction_type=side,
                quantity=qty,product='MIS',status='COMPLETE',filled_quantity=qty,average_price=price))
            writers.append(self.writer());return 'ACK-1'
        self.a.place=place
        try:
            with self.assertRaises(sqlite3.OperationalError):self.e.submit(p,'BUY',100,100.,'Test accepted order')
            self.assertEqual(len(accepted),1)
            order=self.e.rows('SELECT * FROM desk_orders')[0]
            self.assertEqual(order['status'],'SUBMITTING');self.assertIsNone(order['broker_id'])
        finally:
            for writer in writers:writer.rollback();writer.close()
        self.a.orders=lambda:accepted
        self.a.holdings=lambda:[dict(product='MIS',exchange='NFO',tradingsymbol='TESTCE',quantity=100)]
        self.e.manage=lambda **kw:None;self.e.maintain_pending_limits=lambda:None
        self.recover();self.recover()
        order=self.e.rows('SELECT * FROM desk_orders')[0]
        self.assertEqual(order['status'],'COMPLETE');self.assertEqual(order['broker_id'],'ACK-1')
        self.assertEqual(len(accepted),1);self.assertEqual(len(self.e.rows('SELECT * FROM desk_orders')),1)
        self.assertEqual(self.e.rows('SELECT qty FROM desk_positions')[0]['qty'],100)
    def test_writer_queue_allows_concurrent_read_and_releases_after_rollback(self):
        ready=threading.Event();release=threading.Event();second=threading.Event()
        def first():
            try:
                with self.a.db() as c:
                    c.execute("INSERT INTO desk_meta VALUES('rolled_back','1')");ready.set();release.wait(1)
                    raise ValueError('rollback fixture')
            except ValueError:pass
        def following():self.e.put('second_writer',True);second.set()
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            a=pool.submit(first);self.assertTrue(ready.wait(1));b=pool.submit(following)
            try:
                started=time.monotonic();self.assertIsNone(self.e.get('rolled_back'));self.assertLess(time.monotonic()-started,.2)
                self.assertFalse(second.is_set())
            finally:release.set()
            a.result(2);b.result(2)
        self.assertIsNone(self.e.get('rolled_back'));self.assertTrue(self.e.get('second_writer'))
    def test_only_sqlite_busy_is_transient(self):
        for exc in (sqlite3.OperationalError('no such table: x'),sqlite3.OperationalError('disk I/O error'),ValueError('database is locked')):
            self.assertFalse(Adapter.database_busy(exc))
        for text in ('database is locked','database table is locked','database schema is locked'):
            self.assertTrue(Adapter.database_busy(sqlite3.OperationalError(text)))

class WebsiteKeyTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=self.tmp.name+'/website_auth.sqlite3'
        self.ns=dict(os=os,sqlite3=sqlite3,secrets=secrets,_SITE_AUTH_DB=self.path)
        fn=next(n for n in old.tree.body if isinstance(n,ast.FunctionDef) and n.name=='_site_secret_key')
        exec(compile(ast.Module(body=[fn],type_ignores=[]),'<site key>','exec'),self.ns)
    def tearDown(self):self.tmp.cleanup()
    def test_secret_survives_concurrent_starts_and_signed_cookie_remains_valid(self):
        from flask import Flask
        with patch.dict(os.environ,{'WEBSITE_SECRET_KEY':''}):
            with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
                keys=list(pool.map(lambda _:self.ns['_site_secret_key'](),range(12)))
            self.assertEqual(len(set(keys)),1);self.assertEqual(len(keys[0]),64)
            self.assertEqual(os.stat(self.path).st_mode&0o777,0o600)
            one=Flask('before');one.secret_key=keys[0]
            cookie=one.session_interface.get_signing_serializer(one).dumps({'website_authenticated':True})
            two=Flask('after');two.secret_key=self.ns['_site_secret_key']()
            self.assertTrue(two.session_interface.get_signing_serializer(two).loads(cookie)['website_authenticated'])
    def test_environment_secret_takes_priority_without_creating_file(self):
        with patch.dict(os.environ,{'WEBSITE_SECRET_KEY':'configured-test-key'}):
            self.assertEqual(self.ns['_site_secret_key'](),'configured-test-key');self.assertFalse(Path(self.path).exists())
    def test_broken_store_does_not_generate_a_replacement_secret(self):
        Path(self.path).write_text('not a database')
        with patch.dict(os.environ,{'WEBSITE_SECRET_KEY':''}),self.assertRaises(sqlite3.DatabaseError):self.ns['_site_secret_key']()

if __name__=='__main__':unittest.main()
