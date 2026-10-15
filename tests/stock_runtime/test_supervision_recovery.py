"""Isolated tests of the shipped supervisor; no broker login or orders."""
import ast
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from http.client import RemoteDisconnected
try:
    import requests
except ModuleNotFoundError:
    # Test runner fallback: pip bundles the real Requests exception classes.
    from pip._vendor import requests

SOURCE=Path(__file__).with_name('backend.py')
if not SOURCE.exists():SOURCE=Path(__file__).resolve().parents[2]/'backend.py'
tree = ast.parse(SOURCE.read_text())
stock = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'StockDeskEngine')
methods = [n for n in stock.body if isinstance(n, ast.FunctionDef) and n.name in
           ('_supervision_transport_error', '_owned_connection_halt', 'supervise')]
clock = SimpleNamespace(now=100.)
ns = {'requests': requests, 'time': SimpleNamespace(monotonic=lambda: clock.now)}
exec(compile(ast.fix_missing_locations(ast.Module(body=[ast.ClassDef(name='Shipped', bases=[], keywords=[], body=methods, decorator_list=[])], type_ignores=[])), '<shipped supervisor>', 'exec'), ns)
base = next(n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == 'Engine')
verify = next(n for n in base.body if isinstance(n, ast.FunctionDef) and n.name == 'verify_positions')
exec(compile(ast.fix_missing_locations(ast.Module(body=[verify], type_ignores=[])), '<shipped verification>', 'exec'), ns)

class Harness(ns['Shipped']):
    verify_positions = ns['verify_positions']
    def __init__(self):
        self.risk_lock = threading.Lock()
        self.data = {'config': {'armed': False}}
        self.positions = [{'id': 'p1', 'mode': 'live', 'exchange': 'NFO', 'contract': 'TESTPE', 'qty': 100}]
        self.pending = []
        self.mismatches = set()
        self.health = {}
        self.events = []
        self.calls = []
        self.error = None
        self.manage_error = None
        self.broker_qty = 100
        self.a = SimpleNamespace(holdings=self.holdings, market_open=lambda: True)
    def get(self, key, default=None): return self.data.get(key, default)
    def put(self, key, value): self.data[key] = value
    def rows(self, *args): return self.pending
    def active(self): return self.positions
    def event(self, *args): self.events.append(args)
    def reconcile(self): self.calls.append('reconcile')
    def maintain_pending_limits(self): pass  # Controller has its own DB/broker regression suite.
    def holdings(self):
        self.calls.append('holdings')
        if self.error: raise self.error
        return [{'product': 'MIS', 'exchange': 'NFO', 'tradingsymbol': 'TESTPE', 'quantity': self.broker_qty}]
    def manage(self, **kwargs):
        self.calls.append('manage')
        if self.manage_error: raise self.manage_error

def disconnected():
    return requests.exceptions.ConnectionError('Connection aborted.', RemoteDisconnected('Remote end closed connection without response'))

class RecoveryTests(unittest.TestCase):
    def setUp(self): clock.now = 100.; self.h = Harness()
    def interrupt_connection(self):
        self.h.error = disconnected(); self.h.supervise()
    def recover(self):
        self.h.error = None; clock.now += 31; self.h.supervise()
    def test_transport_classification_and_nested_cause(self):
        for error in [disconnected(), requests.exceptions.Timeout(), RemoteDisconnected(), ConnectionResetError(), TimeoutError()]:
            self.assertTrue(self.h._supervision_transport_error(error))
        wrapped = RuntimeError('wrapper'); wrapped.__cause__ = disconnected()
        self.assertTrue(self.h._supervision_transport_error(wrapped))
        for error in [ValueError('invalid positions'), TypeError('programming bug')]:
            self.assertFalse(self.h._supervision_transport_error(error))
    def test_pause_and_automatic_recovery_with_real_position_verification(self):
        self.interrupt_connection()
        self.assertIn('automatic retry in 5 seconds', self.h.get('halt'))
        self.assertEqual(self.h.mismatches, {'p1'})
        self.assertNotIn('manage', self.h.calls)
        self.recover()
        self.assertIsNone(self.h.get('halt'))
        self.assertEqual(self.h.mismatches, set())
        self.assertEqual(self.h.calls[-3:], ['reconcile', 'holdings', 'manage'])
        self.assertFalse(self.h.get('config')['armed'])
        self.assertFalse(self.h.risk_lock.locked())
    def test_backoff_caps_at_30_seconds(self):
        for seconds in [5, 10, 20, 30, 30]:
            self.interrupt_connection()
            self.assertEqual(self.h._supervision_retry_at, clock.now + seconds)
            count = len(self.h.calls)
            self.h.supervise()
            self.assertEqual(len(self.h.calls), count)
            clock.now += seconds
    def test_legacy_screenshot_error_recovers(self):
        self.h.put('halt', 'Position supervision error: ' + str(disconnected()))
        self.h.supervise()
        self.assertIsNone(self.h.get('halt'))
    def test_pending_order_prevents_connection_halt_clear(self):
        self.interrupt_connection(); self.h.pending = [{'id': 'uncertain'}]; self.recover()
        self.assertTrue(self.h._owned_connection_halt(self.h.get('halt')))
    def test_quantity_mismatch_replaces_connection_halt_and_remains(self):
        self.interrupt_connection(); self.h.broker_qty = 0; self.recover()
        self.assertIn('Broker/local position mismatch', self.h.get('halt'))
        self.assertEqual(self.h.mismatches, {'p1'})
    def test_other_halts_are_preserved_across_failure_and_recovery(self):
        for halt in ['Daily loss reached', 'Order submission uncertain', 'Broker reconciliation: no unique match']:
            self.h = Harness(); self.h.put('halt', halt)
            self.interrupt_connection(); self.assertEqual(self.h.get('halt'), halt)
            self.recover(); self.assertEqual(self.h.get('halt'), halt)
    def test_flat_recovery_requires_successful_fresh_broker_read(self):
        self.h.positions = []
        self.h.put('halt', 'Position supervision error: ' + str(disconnected()))
        self.interrupt_connection()
        self.assertTrue(self.h._owned_connection_halt(self.h.get('halt')))
        self.recover(); self.assertIsNone(self.h.get('halt'))
        self.assertEqual(self.h.calls.count('holdings'), 2)
    def test_deferred_verification_does_not_clear_or_manage(self):
        self.interrupt_connection(); clock.now += 31
        self.h.error = type('BrokerDeferred', (Exception,), {})('quota pause')
        self.h.supervise()
        self.assertTrue(self.h._owned_connection_halt(self.h.get('halt')))
        self.assertNotIn('manage', self.h.calls)
    def test_programming_failure_remains_visible_after_success(self):
        self.h.manage_error = ValueError('invalid response')
        self.h.supervise()
        expected = self.h.get('halt')
        self.assertEqual(expected, 'Position supervision error: invalid response')
        self.h.manage_error = None; self.h.supervise()
        self.assertEqual(self.h.get('halt'), expected)
    def test_manage_transport_failure_recovers(self):
        self.h.manage_error = disconnected(); self.h.supervise()
        self.assertTrue(self.h._owned_connection_halt(self.h.get('halt')))
        self.h.manage_error = None; self.recover()
        self.assertIsNone(self.h.get('halt'))
    def test_busy_risk_lock_returns_without_calls(self):
        self.h.risk_lock.acquire(); self.h.supervise()
        self.assertEqual(self.h.calls, [])
        self.h.risk_lock.release()

if __name__ == '__main__': unittest.main(verbosity=2)
