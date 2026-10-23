# Stock database and connection recovery

Build: `2026.10.23-stock-connection.6`

## Problem and resulting behaviour

A temporary SQLite `database is locked` error during position supervision used
to become a persistent `Position supervision error` halt. Once the lock was gone,
that saved halt still blocked qualified entries. A local database error while
applying a broker reconciliation result could also be mistaken for a broker
mismatch and disarm entries.

Stock now queues its in-process ledger writes, while WAL readers remain separate.
Diagnostic batches are reduced from 128 to 32 items per type. Writer queue waits
and SQLite waits are each bounded to 1.5 seconds. This does not remove SQLite's
single-writer constraint or retry an entire trading transaction.

Transient database failures set an in-memory recovery state rather than writing
another error to the unavailable database. Supervision checks retry with a bounded
1–5 second backoff, subject to the existing worker cadence. A successful pass
checks write availability, reconciles orders, verifies positions and runs normal
position supervision. It then wakes entry execution to recheck candidates. A
new concurrent database failure cannot be erased by an older recovery pass.

Legacy database supervision halts clear automatically only after successful
checks, with no unresolved live orders or position mismatches. A compare-and-set
update preserves a newer halt. Paper-only database recovery does not require a
live holdings request. Daily risk limits, explicit disarm, unknown broker orders,
quantity mismatches and genuine validation failures remain effective. No order
intent is deleted or blindly resubmitted. An already accepted order is recovered
from broker evidence, including its saved unique tag.

The Stock dashboard now distinguishes database recovery from dashboard/network
connectivity. The strategy, ML selection, trade sizing, stop/target calculations,
daily protection and Index trading classes remain unchanged.

## Website and broker connection checks

Without `WEBSITE_SECRET_KEY`, the previous backend created a fresh signing key on
each process start. The fallback key is now persisted atomically in the private
`WEBSITE_AUTH_DB` file (default `website_auth.sqlite3`, permissions 0600). An
explicit environment key still takes precedence. A broken key store fails startup
rather than silently generating another key. The existing session lifetime,
password check, CSRF checks and global device revocation remain enabled.

The browser redirects to website login only for an explicit
`website_login_required` response. A proxy 401, timeout or 503 is retried. Broker
status checks share one in-flight request, have an eight-second timeout and retry
transient failures without replacing the last controls with a false logout.
Confirmed broker expiry continues to require reconnection.

## Deployment

1. Deploy `backend.py` and `index.html` together, then restart the Oracle backend.
   This source update does not itself restart the VM service.
2. Preserve all runtime databases and the existing environment configuration.
   Keep `WEBSITE_SECRET_KEY` stable, or preserve the private auth database between
   deployments. With a previously random signing key, one website login after
   this upgrade is expected. Never commit the auth database or a signing key.
3. Use one backend trading process with threads. Engines and Kite session state
   are in process memory; multiple independent trading workers are not made safe
   by this SQLite writer queue. Do not run an old and new backend concurrently.
4. Verify the displayed build and recovery status. Startup still follows the
   existing disarm/scheduled-start rules. Broker access tokens remain in memory;
   a backend restart or actual Kite token expiry may require Kite reconnection.
5. If contention persists, inspect the VM's service/process logs and any external
   writer to `stock_options_ai.db`. The actual VM lock holder was not available
   during this review. Never delete trading databases to clear a lock.

## Verification

- 149 Python regression tests pass, including real SQLite writer contention,
  post-recovery paper execution, resumed stop exits, rollback, persistent legacy
  halt recovery, concurrent halt preservation, and a lock after broker acceptance.
- Full-module Flask smoke test passes with all external requests disabled.
- Browser tests cover dashboard recovery, timeout/503/malformed responses,
  concurrent session checks, confirmed broker expiry and website revocation.
- Index trading source/defaults and its UI script are checked unchanged.

From `tests/stock_runtime`, with the existing Python dependencies installed:

```sh
python -m unittest discover -p 'test_*.py' -q
python smoke_stock_runtime.py
node test_stock_ui.mjs
node test_stock_session_ui.mjs
```

The browser harness uses `happy-dom` at `.qa_node/node_modules/happy-dom` beside
the tests; run the smoke test first to generate its disposable UI fixture. Tests
use fake broker records and do not submit real orders. These are reliability
changes, not evidence of improved trading returns or guaranteed fills.
