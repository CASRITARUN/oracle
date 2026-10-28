"""Index Autotrader (IAT) regressions -- signal model, gates, sizing, exits.

Synthetic candles only.  These prove the engine behaves as specified; they make
no claim about profitability.
"""
import math
import unittest
from datetime import timedelta

import test_algo_upgrade as old

for _name in ("iat_ts", "iat_finite", "iat_clamp", "iat_today", "iat_ema", "iat_rsi",
              "iat_atr", "iat_vwap", "iat_signal", "iat_entry_allowed", "iat_exit_reason",
              "iat_size", "iat_block_reason", "iat_load_state", "iat_load_trades"):
    old.load(_name)

ns = old.ns
iat_signal = ns["iat_signal"]
iat_entry_allowed = ns["iat_entry_allowed"]
iat_exit_reason = ns["iat_exit_reason"]
iat_size = ns["iat_size"]
iat_block_reason = ns["iat_block_reason"]
iat_ema = ns["iat_ema"]
iat_rsi = ns["iat_rsi"]
iat_atr = ns["iat_atr"]
cfg = dict(ns["IAT_DEFAULTS"])


def candles(drift=0.0, noise=0.0, n=120, base=22000.0, seed=7):
    """Deterministic synthetic 5-minute bars."""
    rows, price, s = [], base, seed
    start = old.clock.now - timedelta(minutes=5 * n)
    for i in range(n):
        s = (s * 1103515245 + 12345) % 2147483648
        jitter = ((s / 2147483648.0) - 0.5) * noise
        price = price * (1 + drift) + jitter
        o = price
        h = price * (1 + abs(noise) * 0.0001 + 0.0004)
        l = price * (1 - abs(noise) * 0.0001 - 0.0004)
        rows.append(dict(ts=(start + timedelta(minutes=5 * i)).isoformat(),
                         open=o, high=h, low=l, close=price,
                         volume=1000 + (s % 900)))
    return rows


class SignalTests(unittest.TestCase):
    def test_steady_uptrend_scores_positive_and_wants_calls(self):
        rep = iat_signal(candles(drift=0.0012, noise=0.5), "NIFTY", cfg)
        self.assertTrue(rep["ready"], rep["reason"])
        self.assertGreater(rep["score"], 0.0, rep)
        self.assertEqual(rep["direction"], "CE")

    def test_steady_downtrend_scores_negative_and_wants_puts(self):
        rep = iat_signal(candles(drift=-0.0012, noise=0.5), "NIFTY", cfg)
        self.assertTrue(rep["ready"], rep["reason"])
        self.assertLess(rep["score"], 0.0, rep)
        self.assertEqual(rep["direction"], "PE")

    def test_flat_market_produces_low_strength(self):
        rep = iat_signal(candles(drift=0.0, noise=0.2), "NIFTY", cfg)
        self.assertTrue(rep["ready"], rep["reason"])
        self.assertLess(rep["strength"], cfg["entry_threshold"],
                        "a flat tape must not look like a signal")

    def test_every_factor_is_bounded_and_reported(self):
        rep = iat_signal(candles(drift=0.0012, noise=0.5), "NIFTY", cfg)
        self.assertEqual(set(rep["factors"]), set(ns["IAT_FACTOR_NAMES"]))
        for name, value in rep["factors"].items():
            self.assertTrue(math.isfinite(value), name)
            self.assertGreaterEqual(value, -1.0, name)
            self.assertLessEqual(value, 1.0, name)

    def test_agreement_counts_factors_sharing_the_composite_sign(self):
        rep = iat_signal(candles(drift=0.0012, noise=0.5), "NIFTY", cfg)
        sign = rep["score"] > 0
        expected = sum(1 for v in rep["factors"].values() if v != 0 and (v > 0) == sign)
        self.assertEqual(rep["agreement"], expected)

    def test_insufficient_history_is_not_ready(self):
        rep = iat_signal(candles(n=30), "NIFTY", cfg)
        self.assertFalse(rep["ready"])
        self.assertFalse(iat_entry_allowed(rep, cfg, set())[0])

    def test_composite_is_the_weighted_mean_of_the_factors(self):
        rep = iat_signal(candles(drift=0.0012, noise=0.5), "NIFTY", cfg)
        w = ns["IAT_FACTOR_WEIGHTS"]
        total = sum(w.values())
        expect = sum(rep["factors"][k] * w[k] for k in w) / total
        self.assertAlmostEqual(rep["score"], expect, places=3)


class IndicatorTests(unittest.TestCase):
    def test_ema_of_constant_series_is_that_constant(self):
        self.assertAlmostEqual(iat_ema([5.0] * 40, 9), 5.0, places=9)

    def test_rsi_is_bounded_and_extreme_on_one_way_moves(self):
        up = [float(i) for i in range(1, 60)]
        self.assertGreater(iat_rsi(up), 99.0)
        down = [float(60 - i) for i in range(1, 60)]
        self.assertLess(iat_rsi(down), 1.0)

    def test_atr_is_positive_on_real_bars(self):
        rows = candles(n=40)
        atr = iat_atr([r["high"] for r in rows], [r["low"] for r in rows],
                      [r["close"] for r in rows], 14)
        self.assertIsNotNone(atr)
        self.assertGreater(atr, 0)

    def test_vwap_is_between_low_and_high(self):
        rows = candles(n=30)
        v = ns["iat_vwap"](rows)
        self.assertGreater(v, min(r["low"] for r in rows))
        self.assertLess(v, max(r["high"] for r in rows))


class EntryGateTests(unittest.TestCase):
    def _ready(self, **over):
        rep = iat_signal(candles(drift=0.0012, noise=0.5), "NIFTY", cfg)
        rep.update(over)
        return rep

    def test_strong_agreeing_signal_in_session_passes(self):
        rep = self._ready()
        old.clock.now = old.clock.now.replace(hour=12, minute=0)
        ok, why = iat_entry_allowed(rep, cfg, set())
        self.assertTrue(ok, why)

    def test_low_strength_is_rejected(self):
        old.clock.now = old.clock.now.replace(hour=12, minute=0)
        ok, why = iat_entry_allowed(self._ready(strength=0.10, score=0.10), cfg, set())
        self.assertFalse(ok)
        self.assertIn("strength", why)

    def test_insufficient_agreement_is_rejected(self):
        old.clock.now = old.clock.now.replace(hour=12, minute=0)
        ok, why = iat_entry_allowed(self._ready(agreement=2), cfg, set())
        self.assertFalse(ok)
        self.assertIn("agree", why)

    def test_blowoff_volatility_is_rejected(self):
        old.clock.now = old.clock.now.replace(hour=12, minute=0)
        ok, why = iat_entry_allowed(self._ready(vol_percentile=0.97), cfg, set())
        self.assertFalse(ok)
        self.assertIn("chase", why)

    def test_symbol_with_an_open_position_is_rejected(self):
        old.clock.now = old.clock.now.replace(hour=12, minute=0)
        ok, why = iat_entry_allowed(self._ready(), cfg, {"NIFTY"})
        self.assertFalse(ok)
        self.assertIn("already open", why)

    def test_protected_opening_window_is_rejected(self):
        old.clock.now = old.clock.now.replace(hour=9, minute=25)
        ok, why = iat_entry_allowed(self._ready(), cfg, set())
        self.assertFalse(ok)
        self.assertIn("protected", why)

    def test_after_squareoff_is_rejected(self):
        old.clock.now = old.clock.now.replace(hour=15, minute=13)
        ok, why = iat_entry_allowed(self._ready(), cfg, set())
        self.assertFalse(ok)
        self.assertIn("session window", why)


class SizingTests(unittest.TestCase):
    def test_sizing_is_whole_lots_and_respects_the_risk_budget(self):
        out = iat_size(150.0, 75, cfg, strength=1.0)
        self.assertGreater(out["lots"], 0)
        self.assertEqual(out["quantity"], out["lots"] * 75)
        risk_per_lot = (150.0 * cfg["stop_pct_of_premium"] / 100.0) * 75
        budget = cfg["capital_per_trade"] * (cfg["risk_per_trade_pct"] / 100.0) * 1.5
        self.assertLessEqual(risk_per_lot * out["lots"], budget + 1e-6)

    def test_stake_is_capped_by_available_capital(self):
        out = iat_size(150.0, 75, cfg, strength=1.0)
        self.assertLessEqual(out["quantity"] * 150.0, cfg["capital_per_trade"] + 1e-6)

    def test_weak_signal_gets_a_smaller_stake_than_strong(self):
        strong = iat_size(150.0, 75, cfg, strength=1.0)
        weak = iat_size(150.0, 75, cfg, strength=0.10)
        self.assertLessEqual(weak["lots"], strong["lots"])

    def test_sub_budget_premium_returns_zero_lots_with_a_reason(self):
        out = iat_size(0.5, 75, cfg, strength=1.0)
        self.assertEqual(out["lots"], 0)
        self.assertTrue(out["reason"])

    def test_depth_can_cap_the_size(self):
        uncapped = iat_size(150.0, 75, cfg, strength=1.0)
        if uncapped["lots"] > 1:
            capped = iat_size(150.0, 75, cfg, strength=1.0, depth_qty=75)
            self.assertEqual(capped["lots"], 1)


class ExitTests(unittest.TestCase):
    def setUp(self):
        self.base = dict(symbol="NIFTY", direction="CE", entry_price=100.0,
                         quantity=75, sl_price=88.0, sl_entry=88.0,
                         target_price=121.6, signal_score=0.6, opened_at=old.clock.now.isoformat(),
                         peak_bid=100.0, breakeven_hit=False, trail_active=False, fees=50.0)
        old.clock.now = old.clock.now.replace(hour=12, minute=0)

    def test_stop_beats_target_when_one_bar_spans_both(self):
        # A bar that gapped through both must resolve conservatively: stop first.
        reason = iat_exit_reason(dict(self.base, target_price=101.0), 85.0, 0.6, cfg)
        self.assertEqual(reason, "Stop reached")

    def test_target_is_taken(self):
        self.assertEqual(iat_exit_reason(dict(self.base), 130.0, 0.6, cfg), "Target reached")

    def test_squareoff_wins_over_holding(self):
        old.clock.now = old.clock.now.replace(hour=15, minute=13)
        self.assertEqual(iat_exit_reason(dict(self.base), 100.0, 0.6, cfg), "Intraday square-off")

    def test_max_hold_wins_when_nothing_else_fires(self):
        older = dict(self.base, opened_at=(old.clock.now - timedelta(minutes=120)).isoformat())
        self.assertEqual(iat_exit_reason(older, 100.0, 0.6, cfg), "Maximum holding time")

    def test_signal_reversal_forces_an_exit(self):
        reason = iat_exit_reason(dict(self.base), 100.0, -0.55, cfg)
        self.assertEqual(reason, "Signal reversed")

    def test_a_weak_reversal_does_not_exit(self):
        self.assertIsNone(iat_exit_reason(dict(self.base), 100.0, -0.20, cfg))

    def test_trailing_stop_ratchets_only_after_activation(self):
        trade = dict(self.base)
        # +0.5R: below the activation threshold, the stop must not move.
        iat_exit_reason(trade, 106.0, 0.6, cfg)
        self.assertAlmostEqual(trade["sl_price"], 88.0, places=6)
        self.assertFalse(trade["trail_active"])
        # +1R and a fresh peak: trail locks in below the peak.
        iat_exit_reason(trade, 112.0, 0.6, cfg)
        self.assertGreater(trade["sl_price"], 88.0)
        self.assertTrue(trade["trail_active"])

    def test_stop_rises_once_the_trade_is_a_winner(self):
        # +1R at bid 112 on a 12-point stop; the protective stop must no longer
        # sit below the initial entry level.
        trade = dict(self.base)
        iat_exit_reason(trade, 112.0, 0.6, cfg)
        self.assertTrue(trade["breakeven_hit"])
        self.assertGreaterEqual(trade["sl_price"], trade["entry_price"])

    def test_breakeven_fires_even_when_trailing_is_disabled(self):
        # Isolate the breakeven rule from the trailing rule: with trailing activation
        # pushed out of reach, only the cost-recovery floor can lift the stop.
        quiet = dict(cfg, trailing_activate_r=10.0)
        trade = dict(self.base)
        iat_exit_reason(trade, 112.0, 0.6, quiet)
        self.assertTrue(trade["breakeven_hit"])
        # entry + round-trip fees per unit
        expected = trade["entry_price"] + trade["fees"] / trade["quantity"]
        self.assertAlmostEqual(trade["sl_price"], round(expected, 2), places=6)
        self.assertGreater(trade["sl_price"], trade["sl_entry"])

    def test_sub_r_bid_leaves_the_stop_alone(self):
        trade = dict(self.base)
        iat_exit_reason(trade, 110.0, 0.6, dict(cfg, trailing_activate_r=10.0))
        self.assertFalse(trade["breakeven_hit"])
        self.assertAlmostEqual(trade["sl_price"], 88.0, places=6)

    def test_unusable_quote_is_reported_not_acted_on(self):
        self.assertEqual(iat_exit_reason(dict(self.base), None, 0.6, cfg),
                         "Position quote unavailable")


class BlockerTests(unittest.TestCase):
    def test_disarmed_blocks(self):
        self.assertIn("disarmed", iat_block_reason({"armed": False}, [], cfg).lower())

    def test_armed_with_budget_is_unblocked(self):
        self.assertIsNone(iat_block_reason({"armed": True, "trades_today": 0,
                                            "realized_r_today": 0.0,
                                            "loss_streak": 0}, [], cfg))

    def test_halt_latches(self):
        self.assertIn("Halted", iat_block_reason({"armed": True, "halt_reason": "boom",
                                                  "trades_today": 0, "realized_r_today": 0.0,
                                                  "loss_streak": 0}, [], cfg))

    def test_daily_loss_limit_blocks(self):
        self.assertIn("Daily loss", iat_block_reason(
            {"armed": True, "trades_today": 0, "realized_r_today": -9.0,
             "loss_streak": 0}, [], cfg))

    def test_trade_cap_blocks(self):
        self.assertIn("trade cap", iat_block_reason(
            {"armed": True, "trades_today": 99, "realized_r_today": 0.0,
             "loss_streak": 0}, [], cfg))

    def test_loss_streak_blocks(self):
        self.assertIn("streak", iat_block_reason(
            {"armed": True, "trades_today": 0, "realized_r_today": 0.0,
             "loss_streak": 9}, [], cfg))

    def test_concurrent_cap_blocks(self):
        open_trade = {"status": "open"}
        self.assertIn("Concurrent", iat_block_reason(
            {"armed": True, "trades_today": 0, "realized_r_today": 0.0,
             "loss_streak": 0}, [open_trade], cfg))


if __name__ == "__main__":
    unittest.main()
