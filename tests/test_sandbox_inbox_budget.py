"""Tests for the worker sandbox, the inbox and the pause at the spend cap.

In its own file so that branches adding tests do not all append to test_churn.py and conflict with each other.
"""

from __future__ import annotations

import datetime as dt  # noqa: F401
import json
import subprocess  # noqa: F401
from pathlib import Path  # noqa: F401

import pytest  # noqa: F401
from test_churn import _git, _run, churn  # noqa: F401  (the fixture and the shared helpers)


# ---------------------------------------------------------------- sandbox, inbox, budget pause (2026-11-13)

def test_the_worker_sandbox_is_off_until_a_host_turns_it_on(churn, monkeypatch):
    assert churn.sandbox_settings({}) is None
    assert churn.session_settings(sandbox=True) == churn.SESSION_SETTINGS  # no config: the session is as before
    box = churn.sandbox_settings({"enabled": True, "allowed_domains": ["example.org", "github.com"],
                                  "allow_write": ["/scratch"], "deny_read": ["/home/x/.ssh"]})
    assert box["enabled"] and box["failIfUnavailable"] and box["allowUnsandboxedCommands"] is False
    net = box["network"]
    assert net["strictAllowlist"] is True and "example.org" in net["allowedDomains"]
    assert net["allowedDomains"].count("github.com") == 1 and "api.github.com" in net["allowedDomains"]
    assert box["filesystem"]["allowWrite"] == [str(churn.V6 / ".git"), str(churn.H), "/scratch"]
    assert box["filesystem"]["denyRead"] == ["/home/x/.ssh"] and box["excludedCommands"] == ["docker *"]
    assert "excludedCommands" not in churn.sandbox_settings({"enabled": True, "excluded_commands": []})
    monkeypatch.setattr(churn, "SANDBOX_CFG", {"enabled": True})
    worker = json.loads(churn.claude_cmd("p", "sonnet", 1.0, sandbox=True)[churn.claude_cmd("p", "sonnet", 1.0).index("--settings") + 1])
    assert worker["sandbox"]["enabled"] and worker["hooks"]  # the hooks stay
    tick = json.loads(churn.claude_cmd("p", "sonnet", 1.0)[churn.claude_cmd("p", "sonnet", 1.0).index("--settings") + 1])
    assert "sandbox" not in tick  # ticks drive churn itself: never sandboxed


def test_hosts_the_sandbox_refused_are_read_from_the_result_text(churn):
    text = ("2) FAILED curl: (56) CONNECT tunnel failed, response 403 (sandbox blocked network-outbound to "
            "example.com:443, not on the allow list)\nand sandbox blocked network-outbound to pypi.example.net, twice: "
            "sandbox blocked network-outbound to example.com:443")
    assert churn.sandbox_blocked(text) == ["example.com", "pypi.example.net"]
    assert churn.sandbox_blocked("all fine") == [] and churn.sandbox_blocked(None) == []


def test_a_budget_capped_run_waits_for_the_owner_then_goes_to_the_tick(churn, monkeypatch):
    t0 = churn.now()
    fresh = {"run_id": "7-a", "status": "budget", "report": "r", "issue": 7, "budget_at": churn.iso(t0)}
    assert churn.budget_holds(fresh)
    assert not churn.budget_holds({**fresh, "budget_released": True})  # `churn budget release`
    assert not churn.budget_holds({**fresh, "status": "done"})
    old = {**fresh, "budget_at": churn.iso(t0 - churn.dt.timedelta(hours=churn.BUDGET_PAUSE_H, minutes=5))}
    assert not churn.budget_holds(old)  # unanswered: the tick takes it, as before
    monkeypatch.setattr(churn, "BUDGET_PAUSE_H", 0)
    assert not churn.budget_holds(fresh)  # turned off in config


def test_the_inbox_lists_everything_that_waits_on_a_person_oldest_first(churn, monkeypatch):
    monkeypatch.setattr(churn, "answered_refs", lambda: {("PR", "9")})
    monkeypatch.setattr(churn, "_run_meta", lambda rid: {"finished": "2026-11-13T10:00Z", "cost_usd": 15.0})
    monkeypatch.setattr(churn, "now", lambda: churn.parse_iso("2026-11-13T10:30Z"))
    state = {"prs": [{"pr": 9, "verdict": "hold", "note": "waits on cics-crucible#97", "reviewed_at": "2026-11-12T01:00Z"}],
             "queue": [{"issue": 5, "task": "Owner ruling for PR #4", "needs_owner": True, "pr": 4},
                       {"issue": 6, "task": "ordinary", "needs_owner": False}],
             "stalled": ["focus starved"], "stalled_at": {"focus starved": "2026-11-11T00:00Z"},
             "experiments": [{"id": "E2", "stage": "designed", "question_file": "e2.md"}],
             "reports_pending": [{"run_id": "7-a", "status": "budget", "report": "r", "issue": 7},
                                 {"run_id": "8-a", "status": "done", "report": "r", "issue": 8}],
             "orphans": ["issue #12"]}
    snap = {"9": {"state": "open", "head": "h"}}
    items = churn.inbox(state, snap)
    assert [x["kind"] for x in items] == ["stalled", "pr-hold", "budget", "ruling", "experiment", "liveness"]
    hold = items[1]
    assert hold["ref"] == "PR #9" and hold["answered"] and "cics-crucible#97" in hold["what"]
    budget = items[2]
    assert budget["ref"] == "run 7-a" and "churn budget continue 7-a" in budget["how"] and "$15.0" in budget["what"]
    assert items[3]["ref"] == "queue #5 (PR #4)" and not items[3]["answered"]
    assert all(x["how"] for x in items) and churn.inbox({}, {}) == []
