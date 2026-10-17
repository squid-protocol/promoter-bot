#!/usr/bin/env python3
"""PreToolUse(Bash) hook for churn's worker and tick sessions: refuse wait loops that can only ever find themselves.

2026-10-17: workers #5053 and #4628 each spun ~1 h in `until ! pgrep -f "evidence.py prove"; do sleep 15; done`.
`pgrep -f` searches whole command lines, and the shell running the loop has the pattern in its own command line, so
the job always looks alive. `while ps aux | grep -q X` has the same flaw (grep finds itself).

Blocked: a while/until loop whose `pgrep -f PAT` or `ps ... | grep PAT` pattern matches the command's own text.
`pgrep -f "[e]vidence.py prove"` passes (the bracket keeps the pattern from matching itself), as does waiting on a PID
(`wait $pid`, `while kill -0 $pid`) or a done-file. Add the comment `# self-wait-ok` to run one anyway.
churn passes this hook to every session it starts (`--settings`, see claude_cmd); the gate ends any loop that slips
through (end_dead_wait_loops).
"""

import json
import re
import sys

LOOP = re.compile(r"\b(?:while|until)\b")
QUOTED = r"""(?:"([^"]+)"|'([^']+)'|([^\s;|&)]+))"""
PROBES = [
    re.compile(r"\bpgrep\s+(?:-\S+\s+)*?-[A-Za-z]*f[A-Za-z]*\s+(?:-\S+\s+)*" + QUOTED),
    re.compile(r"\bps\b[^|;]*\|\s*grep\s+(?:-\S+\s+)*" + QUOTED),
]


def self_matching(cmd: str) -> str | None:
    """The pattern of the first wait probe in a loop that matches the command's own text, else None."""
    if not LOOP.search(cmd):
        return None
    for probe in PROBES:
        for m in probe.finditer(cmd):
            pat = next(g for g in m.groups() if g)
            try:
                hit = re.search(pat, cmd)
            except re.error:
                hit = pat in cmd
            if hit:
                return pat
    return None


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except json.JSONDecodeError:
        return 0
    cmd = (data.get("tool_input") or {}).get("command") or ""
    if "self-wait-ok" in cmd:
        return 0
    pat = self_matching(cmd)
    if pat:
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": (
                f"Blocked a wait loop that matches itself: the pattern \"{pat}\" is in this command's own text, so "
                "the shell running the loop always finds itself and the loop never ends (2026-10-17: two churn "
                "workers waited ~1 h on jobs that had already finished). Wait on the PID instead (start the job "
                "with `& pid=$!`, then `wait $pid` or `while kill -0 $pid 2>/dev/null; do sleep 15; done`), on a "
                "done-file the job writes, or bracket the first letter (`pgrep -f \"[e]vidence.py prove\"`). Add "
                "`# self-wait-ok` only if you are sure."),
        }}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
