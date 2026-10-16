# Stock execution and display recovery — 16 October 2026

Runtime build: `2026.10.16-stock-execution.3`.

The screenshots show READY rows while the scan summary says READY 0 / fresh 0, execution is IDLE, no position is shown, and a general connection banner appears. The decision log also contains a manual disarm. A screenshot cannot establish whether entries were still armed at the time or identify the production network failure. No VM logs or broker account were accessed for this repair.

## Confirmed code faults and changes

1. **History refresh delayed option selection.** The scanner refreshed a batch of 28 symbols serially before checking already-fresh candidates. Stock now refreshes history in one separate, paced worker. Fresh candidates go to option checks and are published progressively. Completed history refreshes wake the scanner; scans are coalesced with a five-second minimum pause. Position supervision and entry execution remain separate. Paper mode now receives the same priority over heavy archive/bootstrap work during market hours as live mode.
2. **A transient entry deferral could wait for another full scan.** The execution worker previously ran only when its event was set. It now wakes independently, with a per-candidate 2–10 second retry backoff. Every attempt revalidates current signal, quotes and controls. Active positions and pending/uncertain orders remain owned and prevent duplicates. This is not a broker-order retry loop.
3. **Cancelled unsent plans could consume cooldown and same-signal protection.** When final validation cancelled a plan before an order intent existed, its row still counted as a previous trade. The Stock runtime now records durable evidence for plans it just cancelled without sending an order. Only those zero-quantity, zero-P&L, zero-fee plans with no order record are excluded from cooldown/same-signal history. Audit rows remain intact. Old records without that proof, and every record with any order intent, still count. A missing order in Kite is never treated as proof of non-placement.
4. **READY rows and counters described different moments.** The display now computes counts from the same signal list. Old qualifications awaiting revalidation, active positions and disarmed/blocked entries are labelled accordingly. Execution states show the reason for waiting rather than returning silently to IDLE. In paper mode the page explains that Arm entries starts simulation; switching mode or a manual disarm does not silently rearm it.
5. **The global connection message came from a separate access check.** Every website request formerly ran CREATE/INSERT statements against the auth database, creating unnecessary writer contention. Normal checks now only read the durable session generation; revocation remains checked on every request. Older authenticated cookies missing the CSRF field recover it on the status endpoint. Auth-store failures return a bounded, explicit 503 and do not authorize access. The banner distinguishes an access-check delay from Stock display/data failures. Rendering exceptions are no longer labelled network failures.
6. **Saved Stock state was hidden when Kite disconnected.** Website-authenticated users can now read the saved Stock ledger and the disconnected status without a Kite token. Controls and execution still require their existing checks. No broker request is made by the state endpoint.
7. **Small premium prices were rounded to whole rupees.** Stock entry/stop/target displays now show two decimals, including the proposed-trade panel.

## Unchanged behavior

- Index strategy, its execution worker, display script and independent risk settings.
- Stock confidence thresholds, model weights, model promotion rules, stop/target calculations, daily loss/profit stages, concentration limits and re-entry intervals for actual orders/trades.
- Manual disarm, one-attempt mode, reconciliation, quote freshness, liquidity, broker-funds and exchange-hour checks.
- No automatic resend of an UNKNOWN order, invented fill, deletion of a pending record or bypass of a daily halt.

## Verification

Passed: **85 Python tests**, **22 DOM checks**, full-module offline smoke with **zero external requests**, Python compilation and inline JavaScript syntax checks.

Tests exercise actual Stock admission, final repricing, SQLite position/order records and paper fills with fake market data. Coverage includes recovery from a failed final quote, retained failed-plan evidence, no duplicate fills, pending/unknown protection, manual disarm, daily halt, execution without a new scan event, slow history concurrent with entry/display, correct freshness counts and nonwriting auth reads with immediate revocation.

Run from the repository root:

```sh
python -m unittest discover -s tests/stock_runtime -p 'test_*.py' -q
python tests/stock_runtime/smoke_stock_runtime.py
npm install --prefix tests/stock_runtime/.qa_node --no-save happy-dom@21.0.1
node tests/stock_runtime/test_stock_ui.mjs
```

The full-module smoke test disables application workers and all external requests. It checks website authentication, old-cookie recovery, readable state without Kite, populated legacy records, JSON serialization, independent risk, disarm and global session revocation. Local tests do not establish VM latency, production connectivity, real fills or profitability.

## Apply

Deploy `backend.py` and `index.html` together, retaining the existing databases and account configuration. Restart the existing backend service, then refresh the browser. Confirm build `2026.10.16-stock-execution.3` is displayed. In **Paper trading**, select **Arm entries** if entries are paused. Paper fills appear in this app's positions/journal; they are not sent to Kite.

If the deployed build still fails, the desk now exposes the specific request/read/render/entry reason. The matching VM traceback or authenticated system-health response is needed to diagnose a remaining infrastructure failure; this patch does not claim to repair an unobserved VM/proxy outage.
