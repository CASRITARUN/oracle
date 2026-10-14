import argparse
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('backup', Path(__file__).with_name('trade_journal_backup.py'))
backup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backup)


class FakeGitHub:
    def __init__(self, private=True):
        self.private = private
        self.files = {}
        self.calls = 0
        self.fail_event = False

    def verify_private(self):
        if not self.private:
            raise RuntimeError('Journal destination must be private; no trade data uploaded')

    def put(self, path, data):
        self.verify_private()
        self.calls += 1
        self.files[path] = data
        if self.fail_event and '/events/' in path:
            self.fail_event = False
            raise RuntimeError('Uncertain upload response')


class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.args = argparse.Namespace(data_dir=self.root, state_dir=self.root/'state', backfill_days=2, max_pages=2, export_only=False)
        self.env = patch.dict(os.environ, {'JOURNAL_GITHUB_REPOSITORY': 'owner/private', 'JOURNAL_GITHUB_BRANCH': 'main', 'JOURNAL_GITHUB_PREFIX': 'trade-journal'}, clear=True)
        self.env.start()
        self.day = backup.dt.datetime.now(backup.IST).date().isoformat()
        for desk, filename in backup.DESKS.items():
            with sqlite3.connect(self.root/filename) as c:
                c.executescript('''CREATE TABLE desk_positions(id TEXT,ts TEXT,symbol TEXT,mode TEXT,contract TEXT,entry REAL,stop REAL,target REAL,qty INTEGER,pnl REAL,fees REAL,status TEXT,exit_ts TEXT,setup TEXT);
                  CREATE TABLE desk_orders(id TEXT,ts TEXT,position_id TEXT,side TEXT,filled INTEGER,notional REAL,status TEXT,broker_id TEXT,error TEXT);
                  CREATE TABLE desk_meta(key TEXT,value TEXT);
                  CREATE TABLE desk_trade_events(id INTEGER PRIMARY KEY,position_id TEXT,ts TEXT,kind TEXT,payload TEXT);''')
                c.execute('INSERT INTO desk_positions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)', ('p', self.day+'T09:20:00', desk, 'live', 'TESTCE', 100, 90, 120, 10, -100, 20, 'CLOSED', self.day+'T10:00:00', json.dumps({'confidence': .7, 'api_secret': 'DO_NOT_COPY'})))
                c.execute('INSERT INTO desk_orders VALUES(?,?,?,?,?,?,?,?,?)', ('o', self.day+'T09:20:00','p','BUY',10,1000,'COMPLETE','broker123','Bearer DO_NOT_COPY'))
                c.execute('INSERT INTO desk_meta VALUES(?,?)', ('shared_risk_day:live:'+self.day, json.dumps({'peak': 5500, 'profit_stage': 1, 'reason': 'daily stop', 'access_token': 'DO_NOT_COPY'})))
                c.execute('INSERT INTO desk_meta VALUES(?,?)', ('kite_session', json.dumps({'access_token': 'DO_NOT_COPY'})))
                c.execute('INSERT INTO desk_trade_events VALUES(?,?,?,?,?)', (1,'p',self.day+'T09:21:00','OBSERVATION',json.dumps({'quote': {'bid': 99, 'ask': 100}, 'signal': {'confidence': .7}, 'error': 'DO_NOT_COPY', 'api_key': 'DO_NOT_COPY'})))

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_copy_both_independent_ledgers_and_daily_state(self):
        gh = FakeGitHub()
        result = backup.run(self.args, gh)
        self.assertEqual(result['status'], 'ok')
        for desk in backup.DESKS:
            data = gh.files['trade-journal/'+desk+'/'+self.day+'/summary.json']
            self.assertEqual(data['positions'][0]['symbol'], desk)
            self.assertEqual(data['positions'][0]['pnl'], -100)
            self.assertEqual(data['orders'][0]['broker_id'], 'broker123')
            self.assertEqual(data['risk_state']['shared_risk_day:live:'+self.day]['peak'], 5500)
        self.assertNotIn('DO_NOT_COPY', json.dumps(gh.files))

    def test_public_destination_refused_before_data(self):
        gh = FakeGitHub(False)
        with self.assertRaisesRegex(RuntimeError, 'private'):
            backup.run(self.args, gh)
        self.assertEqual(gh.files, {})

    def test_retry_uncertain_upload_replays_exact_chunk_with_new_events(self):
        gh = FakeGitHub(); gh.fail_event = True
        first = backup.run(self.args, gh)
        self.assertEqual(first['status'], 'partial')
        self.assertTrue((self.args.state_dir/'outbox-stock.json').exists())
        with sqlite3.connect(self.root/backup.DESKS['stock']) as c:
            c.execute('INSERT INTO desk_trade_events VALUES(?,?,?,?,?)', (2,'p',self.day+'T09:22:00','FILL','{"filled_quantity":10}'))
        second = backup.run(self.args, gh)
        self.assertEqual(second['status'], 'ok')
        self.assertIn('trade-journal/stock/events/1-1.json', gh.files)
        self.assertIn('trade-journal/stock/events/2-2.json', gh.files)
        self.assertFalse((self.args.state_dir/'outbox-stock.json').exists())
        self.assertEqual(json.loads((self.args.state_dir/'state.json').read_text())['cursors']['stock'], 2)

    def test_no_incremental_resend_after_ack(self):
        gh = FakeGitHub(); backup.run(self.args, gh)
        gh.files.clear(); backup.run(self.args, gh)
        self.assertFalse(any('/events/' in p for p in gh.files))

    def test_missing_database_is_not_created_and_other_desk_continues(self):
        (self.root/backup.DESKS['stock']).unlink()
        gh = FakeGitHub(); result = backup.run(self.args, gh)
        self.assertEqual(result['missing_databases'], ['stock'])
        self.assertIn('index', result['cursors'])
        self.assertFalse((self.root/backup.DESKS['stock']).exists())

    def test_read_only_query_cannot_modify_positions(self):
        with backup.database(self.root/backup.DESKS['stock']) as c:
            with self.assertRaises(sqlite3.OperationalError):
                c.execute('DELETE FROM desk_positions')

    def test_exit_day_includes_overnight_entry(self):
        yesterday = (backup.dt.datetime.now(backup.IST).date()-backup.dt.timedelta(days=1)).isoformat()
        with sqlite3.connect(self.root/backup.DESKS['stock']) as c:
            c.execute('UPDATE desk_positions SET ts=?', (yesterday+'T15:00:00',))
        with backup.database(self.root/backup.DESKS['stock']) as c:
            self.assertEqual(len(backup.summary(c,'stock',self.day)['positions']),1)

    def test_local_export_without_credentials_does_not_advance_remote_cursor(self):
        self.args.export_only = True
        result = backup.run(self.args)
        self.assertEqual(result['status'], 'ok')
        self.assertFalse((self.args.state_dir/'state.json').exists())
        self.assertTrue((self.args.state_dir/'exports/trade-journal/stock/events/1-1.json').exists())

    def test_changed_destination_cannot_replay_old_pending_outbox(self):
        gh = FakeGitHub(); gh.fail_event=True; backup.run(self.args, gh)
        os.environ['JOURNAL_GITHUB_REPOSITORY'] = 'owner/another-private'
        result = backup.run(self.args, gh)
        self.assertEqual(result['status'], 'partial')
        self.assertTrue(any('Destination changed' in x for x in result['errors']))

    def test_selected_payloads_redact_token_and_drop_unknown_fields(self):
        value = backup.clean({'reason':'Bearer SECRET', 'password':'SECRET', 'quote':{'bid':1,'account_id':'SECRET'},'error':'SECRET'})
        self.assertEqual(value, {'reason':'[REDACTED]', 'quote':{'bid':1}, 'error_category':'other'})

    def test_connection_error_category_without_sensitive_message(self):
        value=backup.clean({'error':'RemoteDisconnected Bearer PRIVATE_TOKEN'})
        self.assertEqual(value, {'error_category':'connection'})

    def test_identical_remote_blob_does_not_put(self):
        data = {'x':1}; raw=backup.encoded(data)
        sha = backup.hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest()
        gh = backup.GitHub('owner/private','main','fake_token')
        calls=[]
        def request(method,suffix,body=None,missing_ok=False):
            calls.append(method)
            return {'sha':sha}
        gh.request=request
        gh.put('journal/example.json',data)
        self.assertEqual(calls,['GET'])

    def test_large_event_pages_split_below_upload_bound(self):
        rows=[dict(id=i,position_id='p',ts=self.day,kind='OBSERVATION',payload=json.dumps({'reason':'x'*2000})) for i in range(500)]
        batches=list(backup.event_batches(rows))
        self.assertGreater(len(batches),1)
        self.assertEqual(sum(len(b) for b in batches),500)
        self.assertTrue(all(len(backup.encoded({'events':b}))<backup.MAX_BYTES for b in batches))


if __name__ == '__main__':
    unittest.main()
