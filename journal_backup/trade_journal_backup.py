#!/usr/bin/env python3
"""Read-only Stock/Index journal mirror. Run separately from the trading process."""
import argparse
import base64
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
DESKS = {'stock': 'stock_options_ai.db', 'index': 'index_options_ai_v1.db'}
MAX_BYTES = 700_000
# Explicit evidence allowlist: arbitrary broker responses, metadata, model payloads,
# free-form errors and account/session objects never leave the VM.
KEYS = set('''id ts symbol mode contract exchange lot qty entry stop target risk status
exit_ts pnl fees reduced exit_reason position_id side broker_id filled notional
limit_price reason direction confidence spot bar_ts bar_age model_id model_ts
score rank_score lots option_delta delta spread_pct bid ask bid_qty ask_qty volume oi
quote_ts exchange_ts source quote signal setup entry_plan risk_plan planned_loss
estimated_costs remaining_qty seconds_since_previous open_pnl_at_bid net_total_at_bid
decision requested_qty filled_quantity average_price price quantity stop_before
stop_after target_before target_after original_qty remaining filled_delta
net peak reserved remaining profit_floor allowed_giveback profit_triggered profit_stage
required_confidence required_entry_confidence liquidate liquidation_pending
entry_block date attempts loss_streak positions missing_quotes config daily_loss
profit_protection_enabled stage1_activation stage2_activation stage1_floor stage2_floor
stage1_confidence stage2_confidence max_positions max_same_direction max_same_sector
max_correlation directional_exposure_limit confidence_threshold armed auto_start
reentry_cooldown cooldown_minutes max_spread fee_per_order daily_risk
historical_success live_option_success success_prob combined_success expected_r
risk_weight blended_confidence portfolio_check used_exposure added_exposure
exposure_limit candidate_delta stressed_delta sector checked_at market open
entry_cash_allocation submitted_price planned_price outcome net_realized
gross_realized estimated_fees realized_pnl order_id verified_at baseline
confirmed filled_total executed_qty exit_qty reason_code resumed_at
entry_ttl_seconds exit_ttl_seconds entry_chase_bps forward_model_max_weight
entry_max_reprices exit_max_reprices limit_reprice_seconds entry_reprice_after_seconds
exit_reprice_after_seconds enabled scope'''.split())
SECRET = re.compile(r'(?:gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+|Bearer\s+\S+|(?:access_token|api_secret|api_key|password|authorization)\s*[=:]\s*\S+)', re.I)


def error_category(error):
    text = str(error).lower()
    for label, terms in (
        ('connection', ('connection', 'remotedisconnected', 'timeout', 'timed out')),
        ('rate_limit', ('rate limit', 'too many requests', '429')),
        ('quote_gap', ('stale', 'quote older', 'fresh quote', 'missing quote')),
        ('funds', ('insufficient funds', 'margin', 'funds')),
        ('reconciliation', ('reconcil', 'mismatch', 'unknown order')),
        ('model', ('model', 'brier', 'auc'))):
        if any(term in text for term in terms):
            return label
    return 'other'


def clean(value, depth=0):
    if depth > 12:
        return None
    if isinstance(value, dict):
        result = {k: clean(v, depth+1) for k, v in value.items() if k in KEYS}
        if value.get('error'):
            result['error_category'] = error_category(value['error'])
        return result
    if isinstance(value, list):
        return [clean(v, depth+1) for v in value[:1000]]
    if isinstance(value, str):
        return SECRET.sub('[REDACTED]', value[:2000])
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    return None


def decoded(value):
    try:
        return clean(json.loads(value))
    except (ValueError, TypeError):
        return {'reason_code': 'invalid_local_json'}


def encoded(value):
    return (json.dumps(value, sort_keys=True, ensure_ascii=False, indent=2, allow_nan=False)+'\n').encode()


def write_local(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(path.name+'.tmp')
    with open(tmp, 'wb') as handle:
        os.chmod(tmp, 0o600)
        handle.write(encoded(payload))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


@contextlib.contextmanager
def database(path):
    # mode=ro avoids creating a missing database; no backend import, Kite login,
    # schema migration, broker action, or trading lock is involved.
    conn = sqlite3.connect(path.resolve().as_uri()+'?mode=ro', uri=True, timeout=1)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA query_only=ON')
    conn.execute('BEGIN')
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()


def summary(conn, desk, day):
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {'desk_positions', 'desk_orders', 'desk_meta', 'desk_trade_events'} <= tables:
        raise RuntimeError('Journal database schema is incomplete')
    positions = []
    for row in conn.execute('SELECT * FROM desk_positions WHERE substr(ts,1,10)=? OR substr(exit_ts,1,10)=? ORDER BY ts,id', (day, day)):
        p = clean(dict(row)); p['setup'] = decoded(row['setup'])
        positions.append(p)
    orders = [clean(dict(r)) for r in conn.execute('SELECT * FROM desk_orders WHERE substr(ts,1,10)=? ORDER BY ts,id', (day,))]
    metadata = {}
    for key in ['config', 'independent_risk_config', 'shared_risk_day:live:'+day, 'shared_risk_day:paper:'+day]:
        row = conn.execute('SELECT value FROM desk_meta WHERE key=?', (key,)).fetchone()
        if row:
            metadata[key] = decoded(row[0])
    return dict(schema_version=1, desk=desk, date=day, timezone='Asia/Kolkata', positions=positions,
                orders=orders, risk_state=metadata,
                limitations='Local evidence, not an independent broker statement. Fees are estimates. Risk configuration is current at export, not necessarily historical. No unrecorded market data can be reconstructed.')


class GitHub:
    def __init__(self, repo, branch, token):
        if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo):
            raise ValueError('Set JOURNAL_GITHUB_REPOSITORY to owner/private-repository')
        if not token:
            raise ValueError('Set JOURNAL_GITHUB_TOKEN on the VM only')
        self.repo, self.branch, self.token = repo, branch, token
        self.root = 'https://api.github.com/repos/'+repo

    def request(self, method, suffix, body=None, missing_ok=False):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.root+suffix, data=data, method=method, headers={
            'Authorization': 'Bearer '+self.token, 'Accept': 'application/vnd.github+json',
            'X-GitHub-Api-Version': '2026-03-10', 'User-Agent': 'oracle-journal-backup/1',
            'Content-Type': 'application/json'})
        # Never forward the credential through an HTTP redirect.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        try:
            with urllib.request.build_opener(NoRedirect).open(req, timeout=15) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            if missing_ok and exc.code == 404:
                return None
            # Do not log API body, headers, request objects, or secrets.
            raise RuntimeError('GitHub HTTP '+str(exc.code)+'; backup will retry on the next timer run') from None
        except (OSError, ValueError):
            raise RuntimeError('GitHub connection/response failed; backup will retry on the next timer run') from None

    def verify_private(self):
        info = self.request('GET', '')
        if info.get('private') is not True:
            raise RuntimeError('Journal destination must be private; no trade data uploaded')

    def put(self, path, payload):
        raw = encoded(payload)
        if len(raw) > MAX_BYTES:
            raise RuntimeError('Journal file exceeds bounded upload size; no evidence was discarded')
        suffix = '/contents/'+urllib.parse.quote(path, safe='/')
        current = self.request('GET', suffix+'?ref='+urllib.parse.quote(self.branch, safe=''), missing_ok=True)
        sha = hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest()
        if current and current.get('sha') == sha:
            return
        self.verify_private()  # Check again immediately before every data write.
        body = dict(message='Mirror '+path+' [skip ci]', branch=self.branch,
                    content=base64.b64encode(raw).decode())
        if current:
            body['sha'] = current['sha']
        self.request('PUT', suffix, body)
        # An uncertain response leaves the cursor unchanged. The next run checks
        # the existing blob hash, avoiding duplicate files/commits after success.


def event_batches(rows):
    batch, size = [], 0
    for row in rows:
        item = dict(id=row['id'], position_id=row['position_id'], ts=row['ts'],
                    kind=row['kind'], evidence=decoded(row['payload']))
        item_raw = encoded(item)
        cost = len(item_raw)+8*len(item_raw.splitlines())+32
        if batch and size+cost > MAX_BYTES-2000:
            yield batch
            batch, size = [], 0
        batch.append(item)
        size += cost
    if batch:
        yield batch


def run(args, github=None):
    state_dir = args.state_dir.resolve()
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(state_dir/'backup.lock', 'a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {'status': 'already_running'}
        repo = os.environ.get('JOURNAL_GITHUB_REPOSITORY', '')
        branch = os.environ.get('JOURNAL_GITHUB_BRANCH', 'main')
        prefix = os.environ.get('JOURNAL_GITHUB_PREFIX', 'trade-journal').strip('/')
        if not prefix or '..' in prefix.split('/') or not re.fullmatch(r'[A-Za-z0-9_./-]+', prefix):
            raise ValueError('Invalid journal path prefix')
        target = repo+'|'+branch+'|'+prefix
        state_path = state_dir/'state.json'
        state = json.loads(state_path.read_text()) if state_path.exists() else {}
        if state.get('target') != target:
            state = {'target': target, 'cursors': {}}
        if not args.export_only:
            github = github or GitHub(repo, branch, os.environ.get('JOURNAL_GITHUB_TOKEN', ''))
            github.verify_private()
        today = dt.datetime.now(IST).date()
        cutoff = (today-dt.timedelta(days=args.backfill_days-1)).isoformat()
        missing, processed, failed = [], {}, []
        def publish(path, data):
            # A locally readable copy remains available even during GitHub outages.
            write_local(state_dir/'exports'/path, data)
            if not args.export_only:
                github.put(path, data)
        for desk, filename in DESKS.items():
            db_path = args.data_dir/filename
            if not db_path.is_file():
                missing.append(desk)
                continue
            try:
                outbox_path = state_dir/('outbox-'+desk+'.json')
                if not args.export_only and outbox_path.exists():
                    pending = json.loads(outbox_path.read_text())
                    if pending.get('target') != target:
                        raise RuntimeError('Destination changed with a pending upload; preserve or archive the local outbox before changing target')
                    publish(pending['path'], pending['payload'])
                    state['cursors'][desk] = max(int(state['cursors'].get(desk, 0)), pending['cursor'])
                    write_local(state_path, state)
                    outbox_path.unlink()
                # Release each read snapshot before HTTP, preventing slow networks
                # from keeping SQLite WAL checkpoints pinned.
                for offset in range(args.backfill_days):
                    day = (today-dt.timedelta(days=offset)).isoformat()
                    with database(db_path) as conn:
                        data = summary(conn, desk, day)
                    publish(prefix+'/'+desk+'/'+day+'/summary.json', data)
                cursor = 0 if args.export_only else int(state['cursors'].get(desk, 0))
                for _ in range(args.max_pages):
                    with database(db_path) as conn:
                        rows = list(conn.execute('SELECT * FROM desk_trade_events WHERE id>? AND ts>=? ORDER BY id LIMIT 500', (cursor, cutoff if not cursor else '')))
                    if not rows:
                        break
                    for batch in event_batches(rows):
                        path = prefix+'/'+desk+'/events/'+str(batch[0]['id'])+'-'+str(batch[-1]['id'])+'.json'
                        payload = dict(schema_version=1, desk=desk, timezone='Asia/Kolkata', events=batch)
                        if not args.export_only:
                            # Persist exact bytes/identity before network: uncertain
                            # writes replay the same chunk even if new events arrive.
                            write_local(outbox_path, dict(target=target, path=path, payload=payload, cursor=batch[-1]['id']))
                        publish(path, payload)
                        cursor = batch[-1]['id']
                        if not args.export_only:
                            state['cursors'][desk] = cursor
                            write_local(state_path, state)
                            outbox_path.unlink()
                processed[desk] = cursor
            except (RuntimeError, OSError, ValueError, sqlite3.Error) as exc:
                # One failed desk does not prevent the other from attempting backup.
                failed.append(desk+': '+(str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__))
        result = dict(status='partial' if failed or missing else 'ok', at=dt.datetime.now(IST).isoformat(),
                      target=repo, cursors=processed, missing_databases=missing, errors=failed,
                      export_only=args.export_only)
        write_local(state_dir/'status.json', result)
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--state-dir', type=Path, default=Path.home()/'.local/state/oracle-journal-backup')
    parser.add_argument('--backfill-days', type=int, default=7)
    parser.add_argument('--max-pages', type=int, default=10)
    parser.add_argument('--export-only', action='store_true')
    args = parser.parse_args()
    if not 1 <= args.backfill_days <= 31 or not 1 <= args.max_pages <= 20:
        parser.error('backfill-days must be 1–31 and max-pages 1–20')
    try:
        result = run(args)
        print(json.dumps(result))
        return 1 if result.get('status') == 'partial' else 0
    except Exception as exc:
        error = str(exc) if isinstance(exc, (RuntimeError, ValueError)) else type(exc).__name__
        with contextlib.suppress(Exception):
            write_local(args.state_dir/'status.json', dict(status='failed', at=dt.datetime.now(IST).isoformat(), error=error))
        print('Backup failed: '+error, file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
