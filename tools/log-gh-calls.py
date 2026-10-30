#!/usr/bin/env python3
"""PreToolUse(Bash) hook for churn's worker sessions: log every `gh` command the worker runs, for the API budget (#64).

The gate measures the account's whole GitHub API burn (`gh api rate_limit`) and attributes churn's own calls
(gh-calls.log); before this hook a worker's calls fell into "other" with the owner's sessions and the workflows.
Each line is `time<TAB>worker:<run id><TAB>kind<TAB>endpoint<TAB>r|w`, the gh-calls.log shape, appended to
$CHURN_GH_LOG. churn sets CHURN_GH_LOG and CHURN_RUN_ID for the sessions it starts (cmd_run).

It never blocks: any error, or no CHURN_GH_LOG, exits 0 with no output. A `gh` inside a loop or a script it runs is
counted once (what the hook sees is the command line, not the calls it makes).
"""

import datetime as dt
import json
import os
import re
import sys

GH = re.compile(r"(?:^|[\s;&|(`$])gh\s+(api|pr|issue|run|repo|release|workflow|search|label|gist|graphql)\b([^;&|\n]*)")
WRITES = re.compile(r"\s(?:-X|--method)\s*(?:POST|PATCH|PUT|DELETE)\b|\s(?:-f|-F|--field|--raw-field|--input)\s")
VERBS_W = {"create", "edit", "merge", "close", "reopen", "comment", "review", "ready", "rerun", "cancel", "delete", "run"}


def lines(cmd: str, run_id: str) -> list[str]:
    """One gh-calls.log line per `gh` invocation in `cmd`."""
    t = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
    out = []
    for m in GH.finditer(cmd):
        kind, rest = m.group(1), m.group(2)
        words = rest.split()
        ep = next((w for w in words if w.startswith(("repos/", "orgs/", "search/", "/"))), "")
        if kind == "api":
            write = bool(WRITES.search(" " + rest))
        else:
            write = bool(words) and words[0] in VERBS_W and not (kind == "workflow" and words[0] != "run")
        out.append(f"{t}\tworker:{run_id}\t{kind}\t{re.sub(r'[0-9]+', 'N', ep.split('?')[0])[:70]}\t{'w' if write else 'r'}")
    return out


def main() -> int:
    log = os.environ.get("CHURN_GH_LOG")
    if not log:
        return 0
    try:
        cmd = (json.load(sys.stdin).get("tool_input") or {}).get("command") or ""
        rows = lines(cmd, os.environ.get("CHURN_RUN_ID") or "?")
        if rows:
            with open(log, "a") as fh:
                fh.write("\n".join(rows) + "\n")
    except Exception:  # noqa: BLE001 -- a logging hook must never stop a worker
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
