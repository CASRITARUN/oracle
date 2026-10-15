# Stock display recovery — 15 October 2026

Runtime build: `2026.10.15-stock-display.2`.
Based on repository commit `772da2e2a33f008dce69314228b14de7e125aa17`.

The reported message, `Display read failed · reference db6fffc65b07`, is generated when the Stock dashboard snapshot worker raises an exception. The reference points to a server-log traceback. It does not itself establish a broker disconnection. The matching VM log was not available in this review, so the exact exception behind that reference has not been confirmed.

Two failure paths were reproduced against the previous code:

- A historical closed paper record without an exit timestamp raises a `TypeError` while calculating its record span. The whole dashboard read was then rejected.
- A runtime NumPy integer in a signal raises a JSON serialization `TypeError`. NumPy booleans, arrays and Python dates/timestamps also needed explicit display encoding.

## Fix

- Keep valid positions, orders and configuration visible when optional historical analytics are incomplete. Unavailable dates, costs and fill measurements are shown as unavailable, with a section warning. Unknown values are never changed to a zero profit/loss or used to claim paper eligibility.
- Normalize supported NumPy values and dates into correctly typed JSON. Preserve unknown financial values as null. Unsupported types still raise a classified diagnostic.
- Publish a fully built risk-display object atomically, avoiding concurrent edits during a dashboard read.
- Add position-history indexes. Increase the background SQLite read budget from 0.6 to 2 seconds; the HTTP request does not wait for this budget.
- Allow the first HTTP request to wait at most 0.1 seconds for a quick initial snapshot. Loading/error responses retry after about one second in the browser. Database retry backoff starts at 0.5 seconds and is capped at five seconds. Only one display read runs at a time.
- Distinguish a failed display read, delayed refresh, optional missing statistics, and a missing Kite sign-in. Return a safe error category, read phase and reference; keep detailed exception contents in server logs.
- Retry when the browser returns online or the visible Stock tab is reopened. Missing current ledger/configuration data still disables entry controls; Disarm remains available.

No order is replayed, deleted, fabricated or marked reconciled by display recovery. No entry rule, trade stop, daily protection, model selection policy or Index engine was changed in this fix. Existing account configuration in the current repository was preserved.

## Checks

- 72 Python tests, including 11 new populated/legacy display regressions.
- 15 DOM checks, including failed-read messaging and recovery.
- Full backend/Flask test using active positions, incomplete historical records and runtime numeric/time values. External requests were blocked; none were attempted.
- Python compilation and inline JavaScript syntax checks.

Run the Python and UI tests using the commands in `STOCK_AI_REVIEW.md`; the new regression module is `tests/stock_runtime/test_stock_display.py`. The smoke check generates its offline UI fixture.

Deploy `backend.py` and `index.html` together and restart the existing backend service. Keep existing databases. The indexes are added automatically. Check runtime build `2026.10.15-stock-display.2`. The authenticated `/api/system-health` response now includes `stock_runtime.display_failure` and `display_phase`.

No VM restart or production timing test was performed here. If the error continues, locate the matching reference in the backend service logs; the newly displayed category and phase help identify whether the remaining issue is database contention, schema mismatch, saved data or serialization.
