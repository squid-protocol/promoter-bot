# promoter-bot

squid-bot's initiation layer (formerly `churn`). In squid-bot's gene-editing frame, the repository's `main` is the
genome and each issue is a known defect to correct or a trait to add. promoter-bot is the inducible promoter that drives
the editors: it decides which edits start, when, and how many at once, and switches editing off when the signals say so.
Its partner, integrase-bot (formerly `shepherd`), screens finished edits and integrates the verified ones into `main`.

What it does, in plain terms:

- **Gate (`promoter-bot tick`, every 10 min from a systemd user timer).** Cheap checks first: stop file, usage-limit
  pause, internet reachability, finished workers, PR state, box capacity (`box_status`). It starts a headless model
  tick only when something needs judgment (a review, a refill, a stalled need).
- **Workers.** Detached headless coding-agent runs (Claude Code; an agy/Gemini lane), each in its own git worktree,
  paced by box capacity and a rolling spend ledger; a usage limit pauses until the reset (weekly resets included).
- **Queue.** Tiered and self-refilling (rules, then a model refill, then standing call-graph work).
- **Housekeeping.** Removes finished worktrees, reaps stale containers, keeps a daily scorecard.
- **Alerts (ntfy).** Gate crashes, Claude or GitHub login failures, disk RED, long or looping usage limits, worker
  failure streaks, owner items waiting 12 h, a stale gate (hourly watchdog), a reboot, and a summary after an
  internet outage.

Installed as `~/.local/bin/promoter-bot -> ./churn`; `~/.local/bin/churn` stays as an alias, and the systemd units,
state files (`churn-state.json`, `churn-log.md`, ...) and `churn/*` branch names keep their old names.
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
  "python": "/data/myproject/venv/bin/python"
}
```

`CHURN_HOME` still overrides the state directory.

Between gate ticks, `churn poll` (every 2 min, `systemd/churn-poll.timer`) makes one gh call and starts the gate at
once when a watched PR changed (a red check labelled needs-fix, a push, a merge), so a fix starts within minutes.
Install: `cp systemd/churn-poll.* ~/.config/systemd/user/ && systemctl --user daemon-reload && systemctl --user enable --now churn-poll.timer`.

Tests: `python3 -m pytest -q tests` (each test runs against a tmp `CHURN_HOME`, never the live state).
