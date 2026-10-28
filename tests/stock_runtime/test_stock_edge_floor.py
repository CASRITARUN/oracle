"""Stock AI live edge-floor regressions.

Paper must stay free to learn; LIVE entries must clear a positive all-in expectation
and, until an option-outcome model is validated, a higher confidence bar. An AUC level
remains a SIZING input on this desk, never an entry cutoff
(see test_stock_logic.test_auc_and_brier_levels_do_not_become_entry_cutoffs).
"""
import json
import unittest
import test_stock_execution as execution

old = execution.old


class EdgeFloorTests(unittest.TestCase):
    def setUp(self):
        execution.ExecutionTests.setUp(self)
        fixed = old.clock.now.replace(hour=14, minute=0, second=0, microsecond=0)
        shift = fixed - old.clock.now
        old.clock.now = fixed
        self.a.q['quote_ts'] = fixed.isoformat()
        for rows in self.a.history.values():
            for row in rows:
                row['ts'] = (old.datetime.fromisoformat(row['ts']) + shift).isoformat()
        for key in ('ts', 'bar_ts'):
            self.seed[key] = (old.datetime.fromisoformat(self.seed[key]) + shift).isoformat()

    def tearDown(self):
        execution.ExecutionTests.tearDown(self)

    # ------------------------------------------------------------- helpers
    def set_mode(self, mode):
        cfg = self.e.config()
        cfg['mode'] = mode
        self.e.put('config', cfg)

    def set_model_auc(self, auc):
        row = self.e.rows('SELECT payload FROM desk_models ORDER BY ts DESC LIMIT 1')[0]
        m = json.loads(row['payload'])
        m['auc'] = auc
        m['ts'] = old.clock.now.isoformat()
        with self.e.db() as c:
            c.execute('UPDATE desk_models SET payload=?', (json.dumps(m),))
        self.e.runtime_cache.clear()

    def gate(self, **overrides):
        s = dict(self.seed)
        s.update(overrides)
        return self.e.entry_quality_gate(s), s

    # ------------------------------------------------------------- tests
    def test_zero_skill_model_still_eligible_but_sizes_to_zero(self):
        # An AUC level must not become an entry cutoff on this desk...
        self.set_model_auc(.50)
        q = self.e.model_quality()
        self.assertTrue(q['live_ok'], q['issues'])
        # ...but a model with no demonstrable edge must not be given size either.
        self.assertEqual(q['risk_weight'], 0.)

    def test_model_that_beats_baseline_keeps_its_size_weight(self):
        self.set_model_auc(.54)
        q = self.e.model_quality()
        self.assertTrue(q['live_ok'], q['issues'])
        self.assertGreater(q['risk_weight'], 0.1)

    def test_wide_spread_kills_live_expectation_that_regulatory_costs_alone_hide(self):
        # Regulatory costs alone say this trade is fine; once the round-trip spread this
        # desk is allowed to cross is priced in, live must reject it.
        self.set_mode('live')
        ok, wide = self.gate(spread=1.0)
        self.assertFalse(ok)
        self.assertIn('floor', wide['reason'])
        self.assertGreater(wide['all_in_costs'], wide['estimated_costs'])
        # The same contract is admitted when the spread is tight, so the rejection is
        # attributable to friction rather than to the setup itself.
        self.set_mode('live')
        ok, tight = self.gate(spread=.1)
        self.assertTrue(ok, tight.get('reason'))

    def test_paper_is_never_vetoed_by_the_live_edge_floor(self):
        self.set_mode('paper')
        ok, s = self.gate(spread=1.0)
        self.assertTrue(ok, s.get('reason'))
        # The expectation is still reported so the desk can display it.
        self.assertIsNotNone(s['option_expected_r'])
        self.assertGreater(s['all_in_costs'], s['estimated_costs'])

    def test_live_entry_before_option_model_needs_higher_confidence(self):
        self.set_mode('live')
        ok, s = self.gate(confidence=.56, p_up=.56, spread=.1)
        self.assertFalse(ok)
        self.assertIn('confidence', s['reason'])
        # The same setup is admitted in paper, where nothing is at risk.
        self.set_mode('paper')
        ok, s = self.gate(confidence=.56, p_up=.56, spread=.1)
        self.assertTrue(ok, s.get('reason'))

    def test_all_in_costs_include_round_trip_friction(self):
        ok, s = self.gate()
        self.assertTrue(ok, s.get('reason'))
        self.assertGreater(s['all_in_costs'], s['estimated_costs'])
        self.assertGreater(s['friction_costs'], 0.)


if __name__ == '__main__':
    unittest.main()
