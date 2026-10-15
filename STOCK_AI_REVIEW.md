# Stock AI runtime update

Build: `2026.10.15-stock-runtime.1`  
Source reviewed: `CASRITARUN/oracle`, parent commit `0c8c73e315fb8d841c5af376ae895c2dc4f6d9b8`.

This update changes Stock AI. Index engine classes, its API handlers and its main UI script are unchanged. Existing Stock and Index risk managers and portfolio controls are unchanged; their budgets remain independent. The shared backend still requires valid environment configuration for both desks.

## Findings and changes

| Finding in the source | Change |
| --- | --- |
| Dashboard reads could invoke trading/risk calculations and many database reads. | A bounded, read-only SQLite snapshot supplies the Stock dashboard. Only one display refresh runs at a time. The last successful response remains visible with its age and stale status. |
| The default Stock tab could initialize before its presentation helpers existed. | Page initialization now runs after the helpers are defined. |
| Scan, supervision and training activity could delay controls. | Disarm sets a local entry pause immediately. Other controls use one bounded command queue, with explicit applied, failed, cancelled or expired status. Disarm also supersedes an Arm request already being processed. |
| Local broker-request deferrals could become persistent supervision errors. | Stock treats local deferrals and rate limits as retryable. Actual order/position reconciliation remains authoritative. |
| Forward settlement and fitting occupied the scanner; old missing outcomes could clog the queue. | A separate Stock learning worker settles local completed candles in batches. Missing data is deferred or marked unavailable, never assigned an invented outcome. |
| Historical retraining could interrupt an existing model. | A persisted active-model ID keeps the current model serving predictions while a replacement is evaluated. Heavy historical fitting is scheduled after market close when a model already exists. |
| Newer models were not necessarily better. | Replacement requires comparison on a shared chronological holdout with at least 200 observations across three sessions. Brier must improve by at least 0.0005 and AUC must not fall by more than 0.01. These are replacement criteria, not new trade-entry restrictions. |
| Forward learning lacked an explicit recent deterioration report. | Predictions are recorded before outcomes. Recent/prior Brier, AUC and accuracy, plus diagnostics by market regime, are displayed. Deterioration requests an after-close model review. |
| Single-candle reversals could cause exit churn. | Direction-reversal exits need two consecutive completed five-minute candles. Price stops, targets, daily protection, square-off and manual close requests retain their existing priority. |
| A large table made the current state difficult to understand. | Stock now has worker/connection status, daily protection, filtered candidates, grouped wait reasons and a detailed learning panel. The full scanner table is expandable. |

## Trading and learning behavior

- Entry eligibility still requires a valid signal, executable market data, available funds, configured portfolio allowance and a reconciled order/position ledger. This update does not force fills or replay orders whose broker status is unknown.
- Existing per-trade stop/target calculations remain based on the trade plan. Daily protection is not used to back-calculate a tighter stop. Existing staged daily profit protection, re-entry cooldown and concentration controls remain in place.
- A completed-candle regime heuristic identifies trend, range and elevated volatility. It makes small ranking adjustments and can reduce whole-lot size in range/volatile conditions; it does not increase size or claim to predict a win probability.
- Forward fitting uses separate chronological fit, calibration and validation windows, with overlapping target horizons removed at the boundaries. The forward contribution remains bounded by the configured cap; recorded comparative outcomes influence that contribution once enough paired observations exist.
- The active model is replaced only after the holdout comparison passes. An older retained model can be marked recently revalidated without rewriting its actual training timestamp. Other structural/model and market-data checks still apply.
- An order already transmitted before Disarm can still fill. Its quantity stays in the ledger and remains supervised. Disarm prevents subsequent local BUY submissions; it is not broker cancellation confirmation.
- Stale dashboard values are labelled and entry controls are disabled on stale display data. The backend supervisor continues independently. A display refresh never places an order.

## Deployment

1. **Rotate the Kite API credentials previously embedded in the public source.** This update removes those defaults, but old Git history still contains them. Set the replacement `KITE_API_KEY` and `KITE_API_SECRET` in the Oracle service environment before restarting; renew broker authorization as required. Existing valid environment settings are read as before.
2. Deploy `backend.py` and `index.html` together. Keep the existing database files and back up the data directory. The Stock observation-column/index migration runs automatically; existing positions, orders and risk settings are retained.
3. Restart the existing backend service using its established single-process setup. Multiple independent engine processes against one broker account are not supported by this update.
4. Check Stock displays runtime build `2026.10.15-stock-runtime.1`, then check connection age, worker status, pending-order reconciliation and risk settings. Existing armed/auto-start settings are retained; a restart may resume entries according to those settings.
5. `/api/system-health` includes the Stock display refresh, learning and control status. The learning panel distinguishes scheduled training, retained models and forward-data gaps.

No Oracle VM restart or live-broker order was performed during this review. Code publication alone does not establish that the VM has deployed it.

## Verification

- 61 Python tests cover execution/reconciliation, supervision recovery and the new Stock runtime, including a stop-trigger exit and concurrent Arm/Disarm handling.
- Offline full-module Flask smoke check: authentication, cold/warm JSON state, independent risk display, Disarm, cache invalidation and health. External HTTP/broker calls are blocked during the check.
- 12 DOM checks cover initial rendering, filters, stale/recovered connections, queued/failed controls, learning diagnostics and unchanged Index script.
- Python compilation and inline JavaScript syntax checks. No real browser rendering or live execution performance test was performed.

Tests are under `tests/stock_runtime/`. Run from the repository root with the backend's Python dependencies installed:

```sh
python -m unittest discover -s tests/stock_runtime -p 'test_*.py' -q
python tests/stock_runtime/smoke_stock_runtime.py
npm install --prefix tests/stock_runtime/.qa_node --no-save happy-dom@21.0.1
node tests/stock_runtime/test_stock_ui.mjs
```

The smoke check generates `smoke_state.json` containing a disposable offline test response; it is not an account snapshot.

## What this review cannot establish

Recent live trade journals, quote/fill timelines, VM resource measurements and service logs were not available. The changes address observed source-code failure paths; they do not prove why a particular day lost money or guarantee future profitability. Forward direction accuracy and AUC do not measure option profit after spread, fees and slippage. The regime detector and reversal confirmation also need observation on actual trades before their performance benefit can be established.

Useful follow-up evidence is the affected trades' entry plans, fills, sampled prices, exit reasons, net costs and backend errors. Share exports privately; do not publish tokens or account details in this public repository.

Technical references: [Kite order lifecycle](https://kite.trade/docs/connect/v3/orders/), [Kite errors and rate limits](https://kite.trade/docs/connect/v3/exceptions/), [chronological validation](https://scikit-learn.org/stable/modules/cross_validation.html).
