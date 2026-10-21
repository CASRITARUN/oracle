# Stock AI evidence and model review — 2026.10.21-stock-evidence.5

Baseline: `CASRITARUN/oracle` main at `a7cbf7ab5f30466c6d9b252004cefb06dc7bf9f9`.

This release implements Stock-only execution evidence, policy-aware option learning, model comparison and rollback, bounded ranking/sizing adjustments, and offline exit research. It does not establish a profitable strategy. No actual losing-day journal, historical option order books, broker account or Oracle VM was available for this review. No live orders were submitted.

## Changes

| Area | Implemented behavior |
| --- | --- |
| Execution visibility | A setup has a stable trace across qualification, final checks, submission, acknowledgment and confirmed fills. Waiting reasons and repeated check attempts are recorded. Paper fills are identified as simulations. Unknown broker outcomes remain unknown and are not retransmitted. |
| Fill quality | BUY fill events include the final observed bid/ask, incremental fill price versus that ask and midpoint, and local confirmation delay measured from order intent. These are observations, not exchange latency or guaranteed executable prices. |
| Position paths | Existing supervision quotes provide sampled net liquidation peaks/troughs, profit giveback and observations near 15/30/60/90 minutes. Realized partial-exit P&L and estimated exit fees are included. Duplicate/stale quotes cannot become new extrema; gaps and restarts are flagged. |
| Option learning | Final entry checks freeze the feature vector, holding time, time to square-off, stop/target, costs and exit-policy version. The label is the recorded option's net outcome under that policy. Legacy records without this snapshot remain in the journal but are excluded from the new fit. |
| Model promotion | Candidates must improve on genuinely later trade windows, with chronological purging, net-outcome evidence, probability-quality checks, session-level uncertainty and drawdown comparison. Failed candidates do not overwrite a valid stored model. Evaluated windows cannot be reused after rollback. |
| Contract selection | Existing whole-lot, funds, spread, depth, freshness and delta checks remain. Validated option EV adds a bounded contract-ranking adjustment. Its influence rises gradually with validation support, from five to at most ten score points. |
| Sizing | A sufficiently supported, recently reviewed losing confidence band may reduce a multi-lot position by at most 25%, rounded to whole lots. It never increases allocation or compresses stop distance. This is a conservative heuristic, not a fitted optimal allocation. |
| Exit research | Two fixed hypotheses—net breakeven after 1R and a 0.75R trail after 1.5R—are replayed over saved quotes. They do not modify live exits. Missing paths are excluded and paths ending before an alternative exit are censored, with no invented P&L. |
| Dashboard/journal | Stock Overview adds “From setup to fill”; Learning lab adds option-model review and exit research. Existing journal reviews and JSON/CSV review exports carry the new evidence. Rollback is available only with an eligible previous model, Stock disarmed and its positions closed/reconciled. |

## Learning requirements and limits

- Paper and live outcomes are separate. Only the active mode is fitted, using at most 4,000 closed positions.
- Entry-time samples are split 60/20/20. Actual exit times plus a five-minute gap purge overlapping trades at the training/calibration/validation boundaries. Comparisons also exclude every previously evaluated holdout window.
- A candidate requires at least 100 training, 30 calibration and 60 new validation trades, both outcome classes in each split and at least six validation sessions. The existing calibration procedure only enables calibration when its own sample requirements pass; otherwise the model is explicitly uncalibrated.
- Promotion requires at least 30 positive-EV selections across six sessions, positive net evidence in three chronological windows, and an approximate lower daily-mean-R bound above zero. Against an incumbent, Brier error must improve by at least 0.0005, selected mean and total net R must not deteriorate, and realized drawdown may not exceed the comparison by more than 0.05R.
- AUC/Brier checks here govern **optional model promotion**, not a new global entry cutoff. Normal direction/liquidity entries continue while this model is learning. Existing live-readiness and financial controls still apply.
- Model evaluation uses the pool of trades actually taken. It cannot prove performance on rejected trades, alternative contracts, alternative sizes or a fully simulated portfolio. R is normalized by each trade's frozen planned risk. Daily-block uncertainty is approximate and does not remove all dependence or selection bias.
- The sizing reduction requires at least 40 qualifying trades and ten sessions in a confidence band over the recent 30-day window, with its approximate upper daily-mean-R bound below zero. Reports older than two days are ignored. A one-lot position is not reduced below one lot.
- The numerical weights and research thresholds are explicit policy choices. Their prospective performance must be measured; they are not claimed to match an institutional trading system or guarantee fewer losses.

## Exit research boundaries

Replays require one confirmed entry fill, the frozen entry policy, sufficient displayed bid depth, and recorded quotes with no gap over 30 seconds or known recorder gap. They use the observed crossing bid with the frozen slippage allowance and estimated fees, never an optimistic fill at the stop level. They use no prices after the actual close. Partial-entry paths and malformed/oversized records are excluded.

Only two saved trades are replayed per background review pass; the UI summarizes the latest 30 closed trades per mode. Results on triggered paths are a selected subset and cannot establish overall policy profitability while other paths are censored/incomplete. Existing adaptive stops, targets, partial exits and reversal confirmation remain active unchanged.

## Responsiveness and retained controls

Execution trace updates are bounded in memory; a separate worker persists snapshots every two seconds. It makes no broker requests and holds no trading lock while writing. The durable order ledger remains authoritative. A crash can lose recent optional diagnostic snapshots, so diagnostics must never be used as proof that an order was not placed. Quote-path observations reuse existing position supervision; no additional market-data request is introduced.

Research and fitting run in existing background paths. Dashboard reads do not train or calculate trading risk, and optional incomplete diagnostic records do not block the dashboard. Model rollback is serialized against option-model training and atomically saves its model/review metadata.

Trade-specific stops and targets remain independent of the remaining daily loss allowance. Editable daily loss/profit floors, simultaneous/directional/industry/correlation/exposure limits, cooldowns, operator disarm, available-cash checks and broker reconciliation remain in force. No risk defaults were changed. No historical journal record was deleted or relabelled.

Only the Stock runtime class and Stock build identifier changed at backend top level. Index strategy/risk definitions and the Index frontend script remain unchanged.

## Verification

- **135 Python tests passed** against production methods with fake brokers and real temporary SQLite ledgers.
- **31 DOM checks passed**, including escaped trace content, mode separation, uncertain-order labels, rollback availability, display recovery and unchanged Index JavaScript.
- Full-module/Flask startup, authentication, typed JSON, legacy ledger display, independent risk, disarm, diagnostics schema and research smoke checks passed with **zero external requests**. Initialization was approximately 1.2 seconds in the test environment; this is not an Oracle VM performance measurement.
- Python compilation, all 20 inline JavaScript syntax checks and the backend scope comparison passed.

Run from the repository root after installing the application's dependencies:

```bash
PYTHONPATH=tests/stock_runtime python -m unittest discover -s tests/stock_runtime -p 'test_*.py' -q
python tests/stock_runtime/smoke_stock_runtime.py
node tests/stock_runtime/test_stock_ui.mjs
```

The DOM harness uses `happy-dom` at `tests/stock_runtime/.qa_node/node_modules/happy-dom`. The smoke test creates its synthetic `smoke_state.json` UI fixture. None of these tests establishes trading profitability or verifies live broker connectivity.

## Applying the update

Update `backend.py` and `index.html` together in the existing Oracle deployment and restart its configured backend service. Confirm the Stock header shows **2026.10.21-stock-evidence.5**. New diagnostic tables are additive and created on startup; preserve the existing SQLite databases and journals. GitHub publication alone does not verify deployment or a VM restart.

New policy-aware samples accumulate from future entries. Prior eligible schema-3 option models can continue until their normal validity expires; missing new samples do not themselves halt ordinary entries. No model was fitted on this user's real trading outcomes during development. Review the actual losing-day journal before attributing losses to a particular signal, exit or execution issue.

## Method references

- [Kite Connect orders: lifecycle, acknowledgment and execution status](https://kite.trade/docs/connect/v3/orders/)
- [scikit-learn: probability calibration and scoring](https://scikit-learn.org/stable/modules/calibration.html)
- [scikit-learn: chronological validation and gaps](https://scikit-learn.org/stable/modules/generated/sklearn.model_selection.TimeSeriesSplit.html)

These references support the methods, not the strategy's profitability.
