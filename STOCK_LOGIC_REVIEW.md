# Stock logic review — 2026.10.20-stock-logic.4

Reviewed baseline: `7112e3ed8f3d0273c8db5ac849f7bca3134d9833`.

This release corrects Stock-only learning, selection and exit defects. It does not establish a profitable strategy or identify the cause of a particular trading day's loss. The accessible repository contains no actual trade journal; `positions.json` is empty. No broker account or Oracle VM was accessed during this review.

## Findings and changes

| Finding in the reviewed code | Correction | Practical effect |
| --- | --- | --- |
| Historical setup labels discarded every path that hit neither barrier. The model estimated success conditional on a barrier being hit, rather than success within the holding horizon. | Complete paths that time out now receive label 0. | Quiet/no-target opportunities are included in training. This does not label their hypothetical option P&L. |
| Historical barrier evaluation could continue across missing candles or into the next session. | Require a complete, contiguous, completed five-minute path within one session. Missing/overnight paths remain unknown and are excluded. Both barriers in one candle still count conservatively as failure. | Overnight gaps cannot become intraday training wins. |
| An underlying-stock target-before-stop probability could dominate option ranking and be blended with a differently defined option-profit probability. | Direction, spread, depth and costs drive the score. Historical setup evidence contributes at most ±2 points. Validated mode-specific option EV contributes at most ±5 points. No averaging of probabilities with different labels. | An optimistic stock-path model cannot overwhelm a substantially better direction/liquidity setup. Score weights are explicit heuristics, not backtested optimal weights. |
| Option training rebuilt old feature vectors using current settings, split by exit order, and did not purge overlapping holding periods. | Freeze the entry feature vector and planned risk at the final entry checks. Split 60/20/20 by entry time and purge using actual exit time plus five minutes at each boundary. | Holdout results cannot use labels that were unavailable when holdout trades began. Legacy rows without frozen features are excluded from fitting, but retained in the journal and results review. |
| One weak candle could tighten a position stop to half its initial distance despite the two-candle reversal rule. | Weakness must appear on two distinct consecutive completed candles; repeated polls of one candle do not count. Recovery resets the confirmation. | A single noisy prediction no longer forces premature tightening. Price stops, targets, daily liquidation and square-off remain immediate. |
| The remaining position's breakeven calculation charged all fees without crediting realized partial-exit profit, and rounded protection downward. | Net breakeven uses `(total fees + remaining exit reserve − realized gross P&L) / remaining quantity`, rounded up to tick. | Partial-exit profit is not charged again to the remaining quantity. Existing stops can only tighten. A breakeven stop remains subject to fills, gaps and costs. |
| Without an eligible option model, a plan's target was not explicitly required to cover estimated costs. | Reject malformed plans and targets with non-positive estimated net proceeds. Display full-quantity net target and stop scenarios. | Costs are visible even while the outcome model is learning. Partial exits can change the realized reward/risk ratio. |

## Learning and model compatibility

- Historical setup models carry a versioned label policy, direction-model identity, reward parameter and holding horizon. Old/incompatible or stale priors are removed from ranking and rebuild through the existing background schedule. Direction and liquidity checks continue while this optional prior is rebuilding.
- Paper and live option-outcome models are trained and stored separately. The active execution mode uses only its own closed outcomes. Live P&L is never mixed with simulated P&L. Training is bounded to the latest 4,000 closed positions in that mode.
- Fitting needs at least 100 usable frozen-entry samples, adequate class coverage in the three windows and at least three validation sessions. These are requirements for using the optional outcome model, not requirements for ordinary direction/liquidity entries.
- Calibration uses the existing disjoint calibration routine when its sample requirements are met. Otherwise its `enabled=false` status is preserved; the result is not represented as calibrated. Predictions are shrunk towards the training win rate according to holdout sample size.
- Outcome-model use requires improvement over its training-prior Brier baseline on the holdout and non-inverted ranking. A model that fails these checks contributes nothing. No hard AUC or Brier level has been reintroduced as a global entry cutoff.
- Direction-model replacement, forward learning, drift review and the configured forward-weight cap are retained. This release does not automatically raise size, loosen risk settings or tune thresholds after a few losing trades.

## Results review

Stock Learning lab now shows a background review of the latest 500 closed positions: net after estimated fees, average net result, gross profits erased by fees, and breakdowns by exit reason, direction and recorded regime. Paper/live results stay separate. Missing amounts or invalid dates are excluded explicitly; a window with no usable records shows unknown amounts. This is descriptive analysis, not a new backtest or broker contract-note reconciliation.

The learner performs this bounded local review at most once per minute. Dashboard requests read the cached report and do not train models or call the broker.

## Retained behavior

Stops and quantity are still based on the trade plan, available cash and executable liquidity, not a share of the remaining daily loss. The editable daily loss/profit floors, position/direction/sector/correlation/exposure limits, re-entry cooldown, operator disarm and existing order-reconciliation controls remain in force. Unknown submissions are never replayed. Existing journal records are not deleted or rewritten as wins/losses.

Only `StockRuntimeEngine` and the Stock build identifier changed in the backend. All other backend definitions, including Index strategy and risk classes, are byte-identical to the baseline. The Index HTML and script are unchanged.

## Verification

Run from the repository root with dependencies installed:

```bash
PYTHONPATH=tests/stock_runtime python -m unittest discover -s tests/stock_runtime -p 'test_*.py' -q
python tests/stock_runtime/smoke_stock_runtime.py
node tests/stock_runtime/test_stock_ui.mjs
```

The DOM test uses `happy-dom`; its existing local dependency path is documented in that script. Offline checks performed for this release:

- **106 Python tests passed**, including 21 new strategy regressions. Counterexamples reproduce the old timeout/overnight-label and ranking defects, then verify the corrected outcomes.
- **24 DOM checks passed**, including separate paper/live results and unchanged Index script.
- Full backend/Flask startup, authentication, populated legacy ledger, typed JSON, risk independence, disarm and health smoke checks passed with **zero external requests**.
- Full backend comparison verified that only the Stock runtime class and build string changed. Python compilation and inline JavaScript syntax checks passed.

These are synthetic-path, accounting, integration and regression tests. They are not an out-of-sample profitability backtest. There was no historical option-book dataset or actual losing-day journal available to support such a claim.

## Deployment and follow-up evidence

Update both `backend.py` and `index.html` together in the existing Oracle deployment, then restart its configured backend service. This GitHub change does not itself verify a VM restart. Confirm the Stock header shows `2026.10.20-stock-logic.4`. Keep the current SQLite databases and journals; do not delete them to clear model/reconciliation messages.

To diagnose the reported losses, review/export the actual losing-day trades and their entry/exit observations. Compare actual quantities, fees, net outcomes, exit reasons, bid/ask paths and model versions. The new results panel helps locate patterns, but cannot reconstruct missing market observations or prove that the strategy will make money.

## Method references

- [scikit-learn: leakage and consistent preprocessing](https://scikit-learn.org/stable/common_pitfalls.html)
- [scikit-learn: chronological splitting and gaps](https://scikit-learn.org/stable/modules/generated/sklearn.model_selection.TimeSeriesSplit.html)
- [scikit-learn: probability calibration and scoring](https://scikit-learn.org/stable/modules/calibration.html)

These references support the validation methodology; they do not validate this trading strategy or its heuristic parameters.
