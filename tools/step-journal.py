#!/usr/bin/env python3
"""PostToolUse(Bash) hook for churn's worker sessions: a journal of the steps a run has finished.

A worker that dies (the box reboots, the network drops, a limit cuts it off) used to leave only what git shows and
whatever handoff note it had managed to write. The run that took over could not tell that the 15-minute gates had
already passed at this commit, and ran them again. This hook appends one JSON line per finished milestone to
$CHURN_JOURNAL (churn sets it to <run dir>/journal.jsonl): a commit, a push, a merge of main, a pull request, the
gates' verdict, the self-check, and any repository tool that ran for a minute or more. Each line carries the commit it
was done at and whether the tree was clean, so a later reader knows whether it still holds.

Claude Code calls a PostToolUse hook only for a command that exited 0, so a line means the command succeeded. A
command piped into `tail` succeeds whatever its first part did; for the gates the verdict is therefore read from the
output, never from the exit status.

It never blocks: any error, or no CHURN_JOURNAL, exits 0 with no output.
"""

import datetime as dt
import json
import os
import re
import subprocess
import sys

SLOW_MS = 60_000  # a repository tool that ran this long is worth not running twice
GATES = re.compile(r"(\d+)/(\d+) gates pass")
SELFCHECK = re.compile(r"selfcheck: (\d+) problem\(s\)")
TOOL = re.compile(r"(?:^|[\s/])tests/tools/(?:[\w-]+/)*([\w-]+)\.(?:py|sh)\b")
PR_URL = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/pull/(\d+)")
STEPS = (
    ("commit", re.compile(r"(?:^|[\s;&|(])git\s+(?:-C\s+\S+\s+)?commit\b")),
    ("push", re.compile(r"(?:^|[\s;&|(])git\s+(?:-C\s+\S+\s+)?push\b")),
    ("merged-main", re.compile(r"(?:^|[\s;&|(])git\s+(?:-C\s+\S+\s+)?merge\s+(?:\S+\s+)*origin/main\b")),
    ("pr", re.compile(r"(?:^|[\s;&|(])gh\s+pr\s+create\b")),
    ("selfcheck", re.compile(r"(?:^|[\s;&|(])churn\s+selfcheck\b")),
)


def _git(cwd: str, *args: str) -> str:
    try:
        return subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, timeout=20).stdout.strip()
    except Exception:  # noqa: BLE001
        return ""


def entries(payload: dict) -> list[dict]:
    """The journal lines one finished Bash call earns (usually none)."""
    cmd = (payload.get("tool_input") or {}).get("command") or ""
    resp = payload.get("tool_response") or {}
    out = f"{resp.get('stdout') or ''}\n{resp.get('stderr') or ''}"
    if not cmd or resp.get("interrupted"):
        return []
    found: list[dict] = []
    if "pr_gates.py" in cmd:
        if m := GATES.search(out):  # no verdict line: the run was cut off or its output hidden, so nothing is claimed
            found.append({"step": "gates", "ok": m.group(1) == m.group(2), "detail": m.group(0),
                          "args": " ".join(re.findall(r"--[\w-]+(?:\s+(?!-)[\w-]+)*", cmd.split("pr_gates.py", 1)[1]))[:120]})
    for name, rx in STEPS:
        if rx.search(cmd):
            e = {"step": name, "ok": True}
            if name == "pr" and (m := PR_URL.search(out)):
                e["detail"] = f"#{m.group(1)}"
            if name == "selfcheck":
                if not (m := SELFCHECK.search(out)):
                    continue  # its verdict line is not in the output: nothing is claimed
                e.update(ok=m.group(1) == "0", detail=m.group(0))
            found.append(e)
    ms = payload.get("duration_ms") or 0
    if ms >= SLOW_MS and "pr_gates.py" not in cmd and (m := TOOL.search(cmd)):
        found.append({"step": f"tool:{m.group(1)}", "ok": True, "detail": f"{ms / 60000:.0f} min",
                      "cmd": " ".join(cmd.split())[:200]})
    if not found:
        return []
    cwd = payload.get("cwd") or os.getcwd()
    head = _git(cwd, "rev-parse", "--short=12", "HEAD")
    clean = not _git(cwd, "status", "--porcelain", "--untracked-files=no")
    t = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
    return [{"t": t, **e, "head": head, "clean": clean} for e in found]


def main() -> int:
    path = os.environ.get("CHURN_JOURNAL")
    if not path:
        return 0
    try:
        rows = entries(json.load(sys.stdin))
        if rows:
            with open(path, "a") as fh:
                fh.write("".join(json.dumps(r) + "\n" for r in rows))
    except Exception:  # noqa: BLE001 -- a journal hook must never stop a worker
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
