# Automatic private trade-journal copy

This separate, standard-library-only Python process reads the existing Stock and
Index SQLite databases without importing the backend or placing broker orders.
It does not restart, arm, disarm, or block the trading process. Stock and Index
retain independent ledgers. A systemd timer runs it every five minutes after the
previous run finishes; this is a delayed evidence backup, not live monitoring.

The `oracle` source repository is public. Do **not** put trade evidence or tokens
there. Create an empty **private** GitHub repository named `oracle-trade-journal`
and initialize it with a README so its `main` branch exists. Grant the ChatGPT
GitHub connection access to that private repository to allow review here.

On GitHub, create a fine-grained token restricted to this new repository, with
Contents read/write permission and an expiry. The VM needs its own token: the
ChatGPT connection does not transfer authentication to your server. Enter it only
in the VM prompt below, never in chat or a committed file.

## One-time VM setup

These paths match the earlier `opc` deployment. Adjust them and the service unit
if your actual deployment differs. Pulling this change adds backup files only;
no trading-service restart is required.

```bash
cd /home/opc/oracle
git pull --ff-only
python3 - <<'PY'
from pathlib import Path
import getpass, os, re
repo = input('Private repository (CASRITARUN/oracle-trade-journal): ').strip() or 'CASRITARUN/oracle-trade-journal'
token = getpass.getpass('Fine-grained GitHub token (hidden): ').strip()
if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo) or not re.fullmatch(r'[A-Za-z0-9_]+', token):
    raise SystemExit('Invalid repository or token; file not saved')
folder = Path.home()/'.config'
folder.mkdir(mode=0o700, exist_ok=True)
path = folder/'oracle-journal-backup.env'
fd = os.open(path, os.O_WRONLY|os.O_CREAT|os.O_TRUNC, 0o600)
with os.fdopen(fd, 'w') as f:
    os.chmod(path, 0o600)
    f.write('JOURNAL_GITHUB_REPOSITORY='+repo+'\nJOURNAL_GITHUB_BRANCH=main\nJOURNAL_GITHUB_TOKEN='+token+'\n')
print('VM credential configuration saved; token not printed')
PY
sudo cp journal_backup/oracle-journal-backup.service /etc/systemd/system/
sudo cp journal_backup/oracle-journal-backup.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl start oracle-journal-backup.service
sudo systemctl status oracle-journal-backup.service --no-pager
sudo systemctl enable --now oracle-journal-backup.timer
```

An `inactive (dead)` oneshot after success is normal. Check its exit code/status,
then verify files appear in the **private** destination before relying on backup.

```bash
sudo journalctl -u oracle-journal-backup.service --no-pager -n 30
systemctl list-timers oracle-journal-backup.timer
cat ~/.local/state/oracle-journal-backup/status.json
```

## Evidence layout

* `trade-journal/stock/YYYY-MM-DD/summary.json` and equivalent `index` paths:
  positions entered or exited that day, orders submitted that day, independent
  risk settings and persisted daily profit peak/floor/halt state.
* `trade-journal/stock/events/FIRST-LAST.json` and equivalent `index` paths:
  incremental recorded observations, entry plans, quote gaps, execution updates
  and exit decisions. Each contains timestamped events and position IDs.
* First run backfills seven days of retained data, including 13 October when run
  on 14 October. Events are uploaded in bounded pages; a large backlog can take
  several runs. Existing incomplete/absent local evidence cannot be reconstructed.
* Summaries for the last seven days are refreshed; older event chunks remain.
  A final historical summary can miss a late correction outside this window.
  Use `--backfill-days 31` for a wider summary refresh when investigating one.
* Paper/live mode and broker order identity are retained. Free-form broker errors,
  account data, credentials, arbitrary metadata and unrecognized payload fields
  are omitted. Recognized token patterns in allowed strings are redacted.
  Errors retain only a fixed category (connection, rate limit, quote gap, funds,
  reconciliation, model or other), never the original free-form error text.
  This is selected evidence, **not** an exact full database/server-log backup.
* Fees are estimates. Current risk configuration is labeled as current; persisted
  daily state supplies recorded peaks/halts, but it is not a continuous equity
  curve. Summaries and event batches are separate short read snapshots, not one
  atomic snapshot of both desks. Always reconcile actual outcomes to Kite.

Uploads use GitHub's Contents API serially. Identical blob hashes avoid unchanged
commits. HTTP/authorization/rate-limit failures leave event cursors unadvanced and
retry on the next timer run, never from a trading thread. Local exports and cursor
state use private file permissions under `~/.local/state/oracle-journal-backup/`.
An exact pending event chunk is persisted before upload. A lost response or process
restart replays that chunk before newer events, even if the live journal grew.
Private-repository checks happen initially and before every write; the script
refuses public repositories. Keep destination visibility private permanently.

Run local-only export (no credentials/GitHub required):

```bash
python3 journal_backup/trade_journal_backup.py --export-only --data-dir /home/opc/oracle
```

Stop backups independently:

```bash
sudo systemctl disable --now oracle-journal-backup.timer
```

This does not give ChatGPT SSH access, deploy code automatically, or establish an
always-running ChatGPT review. Ask for a date review once the files are present.
Protect the private repository and token; rotate the VM token when it expires.
