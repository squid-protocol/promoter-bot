# promoter-bot

**The admission gate and launcher of [squid-bot](https://github.com/squid-protocol/squid-bot): it decides which issues get worked, when, and how many at once, starts headless coding agents on them, and stops starting them when the host, CI, the budget or the network says so.**

Its partner, integrase-bot (in the squid-bot repo, formerly `shepherd`), screens the finished pull requests and merges the verified ones. The squid-bot README has the picture of the whole loop, the numbers it produces, and a table of every control loop with its rule and the incident that set it.

> **Status: one repository's production system, published.** promoter-bot runs [GitGalaxy](https://github.com/squid-protocol/gitgalaxy) every day, and it is still tied to it: it calls that repository's gate scripts (`tests/tools/pr_gates.py`, `tests/tools/box/heavy-run.sh`) and a private agent skill for its model ticks. Read it for the design and the code; expect work before it runs anywhere else. The program is one file, `churn`, which was its name before the rename; the state files, systemd units and `churn/*` branches keep that name.

## Requirements

- Linux with systemd user units, Python 3.10+, `git`, and the GitHub CLI (`gh`) signed in.
- [Claude Code](https://claude.com/claude-code) signed in (the workers are `claude -p` sessions).
- A target repository with labelled issues, and the merge bot from squid-bot to merge what the workers open.

## Install

```bash
git clone https://github.com/squid-protocol/promoter-bot ~/.local/src/promoter-bot
ln -s ~/.local/src/promoter-bot/churn ~/.local/bin/churn
ln -s ~/.local/src/promoter-bot/churn ~/.local/bin/promoter-bot
mkdir -p ~/.config/promoter-bot && $EDITOR ~/.config/promoter-bot/config.json   # see the keys below
```

Then start the gate: [`systemd/first-install/`](systemd/first-install/README.md) has the timer units and the commands. `churn status` shows what is running, queued and spent; `churn inbox` lists what waits on you; `churn stop --hours 8 --reason "..."` pauses it.

## What the workers can touch

Workers are not confined in a container. Each runs as the user who started the gate, in its own git worktree, under Claude Code's `auto` permission mode. Three settings narrow that, all off by default:

- **A separate GitHub identity for workers.** `"worker_token_file": "~/.config/promoter-bot/worker.token"` (a file only you can read) makes every worker's `gh` and `git push` use that token instead of your own login. Use a bot account, or a fine-grained token limited to the one repository with contents, pull requests and issues read/write and nothing else. The gate's own review and merge decisions keep your login, so the account that writes a change is not the account that approves it. If the file is missing, empty or readable by others, the worker does not start.
- **A sandbox.** `"sandbox": {"enabled": true}` turns on Claude Code's Bash sandbox for workers: outbound connections only to an allowlist, writes only in the worktree, the temp directory and listed paths. `churn sandbox probe` checks a host first. docker cannot run inside it and is excluded by default, so a worker that uses docker is not contained.
- **Collaborators only.** Workers read issue and comment text. On a public repository, restrict who can open issues that reach the queue, or review what is queued: text from a stranger is input to a program with a shell.

## When a worker dies

A run that ends without a result (its process vanished, the network dropped, a usage limit cut it off) is taken over in its own worktree. A hook records each finished step (commits, the push, the pull request, the gates' verdict) with the commit it was done at, and the run that takes over is told what is finished and must not be done again. `churn steps RUN` shows that list for any run.

## What it does

- **Gate (`promoter-bot tick`, every 10 min from a systemd user timer).** Cheap checks first: stop file, usage-limit
  pause, internet reachability, finished workers, PR state, box capacity (`box_status`). It starts a headless model
  tick only when something needs judgment (a review, a refill, a stalled need).
- **Workers.** Detached headless coding-agent runs (Claude Code; an agy/Gemini lane), each in its own git worktree,
  paced by box capacity and a rolling spend ledger; a usage limit pauses until the reset (weekly resets included).
- **Queue.** Tiered and self-refilling (rules, then a model refill, then standing call-graph work).
- **Goal (`churn goal`, owner 2026-10-21).** An ordered list of stages (default `translate > l5 > breadth`), each an
  issue set. The active stage's issues launch first in every pick (PR fixes, the core slot, normal work) and lead the
  rule refill; later stages follow, then everything else. With `goal_reports` set, `churn goal measure` (the gate every
  6 h) reads the repo's evidence reports, counts programs per level, writes `progress-snapshots/goal-*.json` and names
  the programs that do not translate whole and no open issue mentions; the model tick is asked to file those. A
  checklist line that merged 3 partial slices is requeued as "SPLIT FIRST" so it gets broken up instead of cycling.
- **Housekeeping.** Removes finished worktrees, reaps stale containers, keeps a daily scorecard.
- **Alerts (ntfy).** Gate crashes, Claude or GitHub login failures, disk RED, long or looping usage limits, worker
  failure streaks, owner items waiting 12 h, a stale gate (hourly watchdog), a reboot, and a summary after an
  internet outage. A stop with no expiry pages every `stop_remind_h` (6).
- **Stop (`churn stop --hours H --reason R` / `churn start`).** A stop lifts itself after `--hours` (default
  `stop_default_h`, 8); `--indefinite` or a bare `touch churn.stop` never expires but keeps reminding the owner.
- **Rescue (`churn rescue` status / `brief` / `now`; off unless `rescue.enabled`).** When no Claude worker has run
  for `rescue.after_h` (2) and nothing legitimate explains it (a stop, a usage-limit or battery pause, offline,
  max_workers 0, box RED), the hourly watchdog starts one detached Opus session as on-call: it diagnoses the idle box,
  uses churn's own levers (unblock waits, queue focus steps, open filler, `churn tick`) and reports to the owner. It
  never lifts a stop, changes limits, merges or edits churn. The host's config orders its choice of work:
  `"rescue": {"enabled": true, "priorities": ["..."], "rules": ["..."], "after_h": 2, "cooldown_h": 4,
  "max_per_day": 3, "model": "opus", "budget_usd": 10}`. A stalled need is asked again after `stall_rearm_h` (2).

Host settings live outside the code in `~/.config/promoter-bot/config.json` (or `$PROMOTER_BOT_CONFIG`); every key is
optional:

```json
{
  "project_root": "/data/myproject",
  "state_dir": "/data/myproject/handoffs",
  "repo_dir": "/data/myproject/repo",
  "worktree_dir": "/data/myproject-worktrees",
  "repo": "your-org/your-repo",
  "dashboard_repo": "your-org/squid-bot",
  "tick_cwd": "/home/you",
  "python": "/data/myproject/venv/bin/python",
  "goal_reports": {"fetch": "generated", "ref": "origin/generated", "path": "docs/language_status/evidence_report"}
}
```

`CHURN_HOME` still overrides the state directory.

Between gate ticks, `churn poll` (every 2 min, `systemd/churn-poll.timer`) makes one gh call and starts the gate at
once when a watched PR changed (a red check labelled needs-fix, a push, a merge), so a fix starts within minutes.
It is installed with the gate ([`systemd/first-install/`](systemd/first-install/README.md)).

Sync pulse: hourly, the gate fetches this install's own checkout and, when origin/main is ahead, runs origin/main's
tests (config `python`, which needs pytest) in a temp worktree, fast-forwards, and installs new or changed
`systemd/` units (enabling new timers). It waits up to 3 h for running workers, never touches a dirty or diverged
checkout (it alerts instead), and alerts once when a main fails its tests here. `churn sync` runs it now; `churn
status` shows the install's version and how far behind it is.

Skills: host-private files live in the private repo `promoter-bot-private`, checked out at
`~/.local/src/promoter-bot-private` (config `private_dir`). The same pulse fast-forwards it to its upstream and links
each `skills/<name>/<file>` into `~/.claude/skills/<name>/<file>` when that file is missing or identical; a copy that
differs is left alone and alerts once a day. `churn skills` runs it now. One-time setup per host:
`gh repo clone squid-protocol/promoter-bot-private ~/.local/src/promoter-bot-private`.

Tests: `python3 -m pytest -q tests` (each test runs against a tmp `CHURN_HOME`, never the live state).

## The RSI lane

The system's weekly look at itself is one lane, on by default and outside the focus, running one job at a time. `churn rsi`
shows it.

- **Retro** (`churn retro`): prunes and promotes the rules workers read, from the week's run data.
- **Conflict job** (`churn rsi conflicts` prints the measure): plan holds are per file, so a file many slices edit makes
  them run one at a time. Each week churn measures, with no model and no GitHub, which files held runs and took commits.
  A code or test file past the thresholds launches one capped Opus session, which splits it without changing behaviour,
  following the repo's `hot-file-split` skill. A week with no candidate spends nothing. Set it up with
  `churn rsi set conflicts issue N`, where N is a standing issue in the target repo.

## The credit lane

Prepaid API credit (`churn credit add USD`; key in `~/.config/promoter-bot/anthropic-credit.key`) runs extra workers
whose `claude -p` bills the API, with their own ledger, floor and `max_running`. `churn credit` shows it.

**Credit ticks** (opt-in, off by default: `churn credit ticks on|off`). While the plan is paused on its limit, churn
normally stops reviewing. With this switch on and the lane funded, the gate runs in full during the pause: the model
tick that reviews, merges and sends back bills the API key (capped per tick), and every Claude launch or send-back while
paused goes on the credit lane (key, credit budget cap, credit ledger) or is refused, never on the plan. The tick's status
file tells it so. RSI and experiment launches wait for the reset. Caps (`churn credit set KNOB V`): `tick_cap_sonnet`
$1.50, `tick_cap_opus` $4, `ticks_per_hour` 3, `tick_day_usd` $25 of ticks per 24 h, plus the lane's `floor_usd` and
`max_running`. With the switch off, no key, a rate-limit hold, or not enough credit for one tick, a pause behaves as it
always has. `churn status` and `churn credit` show the switch, its spend, and whether it is running now.

## The solver lane

A token-free lane (owner 2026-11-02; off by default: `churn solver on`). It runs gitgalaxy's
`tests/tools/strengthen.py` with `tests/tools/branch_solver.py backend` as the input source instead of a model, so new
equivalence inputs cost no tokens. `churn solver` shows it (`pick` names the next case; `forget CASE`, `set KNOB V`).

- **Gate (each tick, also during a plan pause and on credit ticks).** Off, workers paused, a run already live, the
  solver's `backend` not on origin/main (the gate's `churn-tools` checkout), no free heavy slot (box GREEN with
  `free_slots` >= 1), `max_per_day` (6) runs in 24 h, `cooldown_min` (30) since the last, or z3 missing in the config
  `python` (re-checked every `z3_recheck_h`, 6): it stays quiet and keeps the reason for `churn solver`.
- **Pick.** Burned-estate call / CICS cases (`estate4_draw.BURNED_NAMES` plus cics-crucible) with open net branch gaps:
  `det_sweep_coverage/<case>.json` uncovered outcomes minus `infeasible_outcomes/<case>.json`, most gaps first. A case
  is skipped while its fingerprint (the blobs of its case, coverage and infeasible records and of the solver and
  strengthen scripts at origin/main) matches its last attempt, so it is tried again only when main changes one of them.
- **Run (`churn _solver RUN`, detached).** A detached gitgalaxy worktree at that main; strengthen (`--rounds 1
  --no-mutation`) under `heavy-run.sh`. Covered branches up and the case still proven: the candidate case goes over
  `case.json` (same indentation), `ladder_refresh.py` refreshes the ledgers, and a DRAFT PR opens (no labels; body =
  strengthen's report plus the refresh log; a failed refresh is flagged at the top). No PR over the 5-file rule or when
  a watched PR or live worker holds the same `case.json`. A port that differs on a new input is a port gap: no PR, the
  owner is told. Every outcome is booked under `solver.attempts`; the worktree goes, and so does the work dir unless a
  person should look at it.
- **Host config (optional).** `"solver": {"env": {"CICS_CRUCIBLE_PATH": "..."}}` in the config file adds environment
  for the run; JAVA_HOME (JDK 17) and the corpora path come from the worker environment.

## License

MIT License. Copyright (c) 2026 Joe Esquibel / Squid Protocol. See [LICENSE](LICENSE).
