"""Tests for churn's state schema, reconcile rules, CLI state edits and scorecard (owner 2026-10-13, items 2-6).

Every test loads a fresh copy of the script with CHURN_HOME pointing at a tmp dir, so nothing touches the live state.
"""

from __future__ import annotations

import argparse
import datetime as dt
import importlib.machinery
import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "churn"


@pytest.fixture
def churn(tmp_path, monkeypatch):
    monkeypatch.setenv("CHURN_HOME", str(tmp_path))
    monkeypatch.setenv("PROMOTER_BOT_CONFIG", str(tmp_path / "no-config.json"))  # defaults, never the host's config
    loader = importlib.machinery.SourceFileLoader("churn_under_test", str(SCRIPT))
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader("churn_under_test", loader))
    loader.exec_module(mod)
    monkeypatch.setattr(mod, "pid_alive", lambda pid: bool(pid) and pid > 0)
    mod.real_issue_tier = mod.issue_tier
    monkeypatch.setattr(mod, "issue_tier", lambda n, meta=None: None)  # never ask GitHub from a test
    mod.real_issue_meta = mod.issue_meta
    monkeypatch.setattr(mod, "issue_meta", lambda n: None)
    mod.real_worktree_pr = mod.worktree_pr
    monkeypatch.setattr(mod, "worktree_pr", lambda wt, issue=None: ("", None))
    monkeypatch.setattr(mod, "ci_triage", lambda n: None)  # unreadable triage: queue as before
    monkeypatch.setattr(mod, "start_precheck", lambda rid: None)
    monkeypatch.setitem(mod.REFILL_DEFAULT, "enabled", False)  # refill tests turn it on; it reads GitHub
    monkeypatch.setattr(mod, "open_issues", lambda: [])
    monkeypatch.setattr(mod, "issues_with_open_prs", lambda: set())
    monkeypatch.setitem(mod.WT_GC_DEFAULT, "enabled", False)  # gc tests call gc_worktrees directly; it reads GitHub
    mod.real_unreachable = mod.unreachable
    monkeypatch.setattr(mod, "unreachable", lambda hosts=mod.NET_HOSTS: [])  # online, without asking the network
    mod.STATE.write_text(json.dumps({"prs": [], "queue": [], "running": [], "reports_pending": [], "usage": {"log": []}}))
    return mod


def put(churn, **state):
    base = {"prs": [], "queue": [], "running": [], "reports_pending": [], "usage": {"log": []}}
    churn.STATE.write_text(json.dumps({**base, **state}))


def item(issue, **kw):
    return {"issue": issue, "model": "sonnet", "task": f"do #{issue}", "needs_owner": False, **kw}


def pr(state="open", head="aaaa11112222", labels=(), ms="clean", branch="churn/1-x", merged=False, closes=False):
    return {"state": state, "merged": merged, "head": head, "mergeable_state": ms, "labels": sorted(labels),
            "branch": branch, "closes": closes}


# ---------------------------------------------------------------- item 2: schema
class TestNormalize:
    def test_the_5019_slip_is_repaired_not_fatal(self, churn):
        st = {"prs": [4937, {"pr": 5019, "issue": 4965}, "5020", {"number": "#5021"}]}
        notes = churn.normalize_state(st)
        assert [churn.pr_num(p) for p in st["prs"]] == [4937, 5019, 5020, 5021]
        assert st["prs"][1] == {"number": 5019, "issue": 4965}
        assert any("repaired prs[1]" in n for n in notes)

    def test_duplicate_prs_keep_the_entry_with_review_facts(self, churn):
        st = {"prs": [5038, {"number": 5038, "reviewed_head": "b3c8"}]}
        churn.normalize_state(st)
        assert st["prs"] == [{"number": 5038, "reviewed_head": "b3c8"}]

    @pytest.mark.parametrize("bad, why", [
        ({"model": "sonnet", "task": "x"}, "not an int"),
        ({"issue": 1, "task": ""}, "no task"),
        ({"issue": 1, "task": "x", "model": "gpt"}, "model"),
        ({"issue": 1, "task": "x", "backend": "gemini"}, "backend"),
        ("not a dict", "not an object"),
    ])
    def test_bad_queue_items_are_quarantined_and_the_rest_survive(self, churn, bad, why):
        st = {"queue": [item(1), bad, item(2)]}
        churn.normalize_state(st)
        assert [q["issue"] for q in st["queue"]] == [1, 2]
        assert why in st["quarantine"][-1]["why"]
        assert st["quarantine"][-1]["value"] == bad

    def test_queue_fields_are_coerced(self, churn):
        st = {"queue": [{"issue": "#7", "task": "x", "backend": "claude", "after_prs": "5038", "pr": "12"}]}
        churn.normalize_state(st)
        assert st["queue"] == [{"issue": 7, "task": "x", "model": "sonnet", "needs_owner": False, "after_prs": [5038], "pr": 12}]

    def test_bad_scalars(self, churn):
        st = {"max_workers": "lots", "paused_until": "soon"}
        churn.normalize_state(st)
        assert st["max_workers"] == 2 and st["paused_until"] is None
        assert {q["where"] for q in st["quarantine"]} == {"max_workers", "paused_until"}

    def test_clean_state_is_untouched_and_idempotent(self, churn):
        st = {"prs": [1, {"number": 2}], "queue": [item(3)], "running": [], "reports_pending": [], "usage": {"log": []}}
        assert churn.normalize_state(st) == []
        assert "quarantine" not in st

    def test_a_malformed_file_loads_and_the_save_logs_the_repair(self, churn):
        put(churn, prs=[{"pr": 5019}], queue=[item(1), {"task": "no issue"}])
        st = churn.load_state()
        assert st["prs"] == [{"number": 5019}]
        with churn.edit_state():
            pass
        log = churn.LOG.read_text()
        assert "STATE repaired prs[0]" in log and "STATE quarantined queue[1]" in log
        assert json.loads(churn.STATE.read_text())["prs"] == [{"number": 5019}]


# ---------------------------------------------------------------- item 3: reconcile
class TestReconcile:
    def test_merged_fixes_needs_no_followup_part_of_does(self, churn):
        st = {"prs": [1, {"number": 2, "issue": 20}, 3, 4], "followups": []}
        snap = {"1": pr("closed", merged=True, closes=True), "2": pr("closed", merged=True), "3": pr("closed"), "4": pr()}
        notes = churn.settle_closed(st, snap)
        assert [churn.pr_num(p) for p in st["prs"]] == [4]
        assert [(f["pr"], f["kind"], f["issue"]) for f in st["followups"]] == [(2, "merged", 20), (3, "closed", None)]
        assert any("closes its issue" in n for n in notes)

    def test_reviews_follow_the_head_not_a_seen_cache(self, churn):
        st = {"prs": [{"number": 1, "reviewed_head": "aaaa11112222"}, {"number": 2, "reviewed_head": "old"}, 3, 4, 5, 6]}
        snap = {
            "1": pr(labels=["shepherd:reapprove"]),          # reviewed at this head: done
            "2": pr(labels=["shepherd:reapprove"]),          # head moved since the review: needs one
            "3": pr(labels=["shepherd:merge"]),              # approved, waiting for CI/shepherd: nothing to do
            "4": pr(labels=[]),                              # no merge label, never reviewed
            "5": pr(labels=["shepherd:merge"], ms="dirty"),  # conflicts: a fix item, not a review
            "6": pr(labels=["shepherd:merge", "shepherd:needs-fix"]),
        }
        assert set(churn.reviews_needed(st, snap, live=[])) == {"2", "4"}

    def test_a_pr_a_live_worker_is_on_needs_nothing(self, churn):
        snap = {"5041": pr(labels=["shepherd:reapprove"], branch="churn/4771-pointer-lane")}
        live = [{"run_id": "r", "pid": 1, "issue": 4770, "task": "Unblock #5038/#5041"}]
        assert churn.reviews_needed({"prs": [5041]}, snap, live) == {}

    def test_pr_fix_items_are_queued_once_for_unattended_conflicts(self, churn):
        st = {"prs": [{"number": 5041, "issue": 4771}], "queue": [], "running": []}
        snap = {"5041": pr(labels=["shepherd:merge", "shepherd:needs-fix"], ms="dirty", branch="churn/4771-pointer-lane")}
        assert churn.queue_pr_fixes(st, snap) == ["#5041"]
        assert churn.queue_pr_fixes(st, snap) == []  # already queued
        q = st["queue"][0]
        assert (q["issue"], q["pr"], q["auto"]) == (4771, 5041, "pr-fix") and "conflicts with main" in q["task"]

    def test_needs_stall_after_three_model_ticks(self, churn):
        st = {}
        for _ in range(churn.STALL_AFTER):
            active, stalled = churn.count_attempts(st, ["review #1@a"])
            assert active == ["review #1@a"] and not stalled
            churn.bump_attempts(st, active)
        active, stalled = churn.count_attempts(st, ["review #1@a"])
        assert active == [] and stalled == ["review #1@a"] and st["stalled"] == ["review #1@a"]
        assert churn.count_attempts(st, ["review #1@a"]) == ([], [])  # reported once
        churn.count_attempts(st, [])  # the need went away: counts and the stall clear
        assert st["attempts"] == {} and st["stalled"] == []


# ---------------------------------------------------------------- item 4: rules in code
class TestGateRules:
    GREEN = {"verdict": "GREEN", "free_slots": 3}

    def test_after_prs_holds_a_claude_launch(self, churn, monkeypatch):
        monkeypatch.setattr(churn, "pace_allows", lambda s, m: (True, "pace off"))
        st = {"queue": [item(10, after_prs=[5038]), item(11)], "running": [], "max_workers": 2}
        reasons, _ = churn.gate(st, self.GREEN, [], {}, {"5038": pr()})
        assert any("#11 (" in r for r in reasons)
        reasons, _ = churn.gate(st, self.GREEN, [], {}, {"5038": pr("closed", merged=True)})
        assert any("#10 (" in r for r in reasons)

    def test_unwatched_after_pr_is_looked_up_once(self, churn, monkeypatch):
        calls = []
        monkeypatch.setattr(churn, "gh_json", lambda path: calls.append(path) or {"state": "closed"})
        assert churn.blocked_by(item(1, after_prs=[9]), {}) == []
        assert churn.blocked_by(item(1, after_prs=[9]), {}) == []
        assert len(calls) == 1

    def test_three_broken_prs_hold_new_work_but_not_their_own_fix(self, churn, monkeypatch):
        monkeypatch.setattr(churn, "pace_allows", lambda s, m: (True, "pace off"))
        snap = {str(n): pr(labels=["shepherd:needs-fix"]) for n in (1, 2, 3)}
        st = {"queue": [item(10)], "running": [], "max_workers": 2}
        reasons, _ = churn.gate(st, self.GREEN, [], {}, snap)
        assert not any("launch slot" in r for r in reasons)
        st["queue"].insert(0, item(1, pr=1))
        reasons, _ = churn.gate(st, self.GREEN, [], {}, snap)
        assert any("PR fix for #1" in r for r in reasons)

    def test_the_cap_comes_from_max_workers(self, churn):
        assert churn.worker_slots(self.GREEN, 0, 1) == 1
        assert churn.worker_slots(self.GREEN, 1, 1) == 0
        assert churn.worker_slots(self.GREEN, 1, 2) == 1
        assert churn.worker_slots({"verdict": "YELLOW", "free_slots": 3}, 0, 2) == 0
        assert churn.worker_slots({"verdict": "GREEN", "free_slots": 2}, 2, 3) == 1  # the third worker
        assert churn.worker_slots({"verdict": "GREEN", "free_slots": 0}, 2, 3) == 0  # the box says no
        assert churn.worker_slots({"verdict": "GREEN", "free_slots": 1}, 0, 3) == 1

    def test_needs_wake_the_model_and_daily_summary_does_not(self, churn):
        st = {"queue": [], "running": [], "last_daily_summary": "2000-01-01"}
        assert churn.gate(st, self.GREEN, [], {}, {})[0] == []
        assert churn.gate(st, self.GREEN, [], {"review #1@a": "PR needs review: #1"}, {})[0] == ["PR needs review: #1"]

    def test_two_rejects_at_a_step_route_it_back_to_claude(self, churn):
        put(churn, queue=[item(1, backend="agy", agy_step=2), item(2, backend="agy", agy_step=3),
                          item(3, backend="agy", agy_step=1)])
        ns = lambda v: argparse.Namespace(issue=9, step=2, verdict=v, pr=None, denials=0, notes="")  # noqa: E731
        churn.cmd_agy_result(ns("REJECT"))
        assert all(q.get("backend") == "agy" for q in churn.load_state()["queue"])
        churn.cmd_agy_result(ns("REJECT"))
        st = churn.load_state()
        assert {q["issue"]: q.get("backend") for q in st["queue"]} == {1: None, 2: None, 3: "agy"}

    def test_pr_reviewed_merge_records_the_head_and_labels(self, churn, monkeypatch):
        labels = []
        monkeypatch.setattr(churn, "gh_label", lambda n, add=None, remove=None: labels.append((n, add, remove)))
        put(churn, prs=[5038])
        churn.cmd_pr(argparse.Namespace(action="reviewed", number=5038, issue=4770, head="b3c81f31", verdict="merge", note=None))
        assert churn.load_state()["prs"] == [{"number": 5038, "issue": 4770, "reviewed_head": "b3c81f31", "verdict": "merge",
                                              "reviewed_at": churn.load_state()["prs"][0]["reviewed_at"]}]
        assert labels == [(5038, "shepherd:merge", "shepherd:reapprove")]

    def test_followup_and_review_done(self, churn):
        put(churn, followups=[{"pr": 7, "kind": "merged"}],
            reports_pending=[{"run_id": "r1", "issue": 1, "status": "done", "report": "x"}])
        churn.cmd_followup(argparse.Namespace(number=7, note="acceptance met; closed #1"))
        churn.cmd_review(argparse.Namespace(run_id="r1", note=None))
        st = churn.load_state()
        assert st["followups"] == [] and st["reports_pending"] == []
        with pytest.raises(SystemExit):
            churn.cmd_review(argparse.Namespace(run_id="r1", note=None))


# ---------------------------------------------------------------- item 6: scorecard
def test_scorecard_counts_the_day(churn):
    day = dt.date(2026, 10, 13)
    t = lambda h: f"2026-10-13T{h:02d}:00:00Z"  # noqa: E731
    pulls = [
        {"number": 1, "head": {"ref": "churn/1-a"}, "merged_at": t(3), "created_at": t(1), "updated_at": t(3)},
        {"number": 2, "head": {"ref": "dependabot/x"}, "merged_at": t(4), "created_at": "2026-10-10T00:00:00Z", "updated_at": t(4)},
        {"number": 3, "head": {"ref": "churn/3-b"}, "merged_at": None, "created_at": t(5), "updated_at": t(5)},
        {"number": 4, "head": {"ref": "churn/4-c"}, "merged_at": "2026-10-12T23:00:00Z", "created_at": "2026-10-12T20:00:00Z",
         "updated_at": "2026-10-12T23:00:00Z"},
    ]

    def gh(path):
        if path.startswith(f"repos/{churn.REPO}/pulls"):
            return pulls if "page=1" in path else []
        if path.startswith("search/issues"):
            return {"total_count": 5 if "created" in path else 7}
        return [{"number": 50, "title": "main is red: Full Suite", "created_at": "2026-10-12T22:00:00Z", "closed_at": t(6)}]

    state = {"usage": {"log": [
        {"t": t(2), "model": "sonnet", "cost_usd": 3.0}, {"t": t(2), "model": "orchestrator-opus", "cost_usd": 1.0},
        {"t": t(2), "model": "agy-gemini", "tokens": 0}, {"t": "2026-10-12T02:00Z", "model": "sonnet", "cost_usd": 99.0}]},
        "queue": [item(1), item(2, needs_owner=True)]}
    log_text = ("- 2026-10-13T03:00Z [churn] reaped r1 (#1 sonnet): done\n"
                "- 2026-10-13T04:00Z [churn] reaped r2 (#2 sonnet): crashed\n"
                "- 2026-10-12T04:00Z [churn] reaped r0 (#0 sonnet): done\n")
    c = churn.scorecard(day, state, gh=gh, log_text=log_text)
    assert c["merged"] == [1, 2] and c["churn_merged"] == [1] and (c["opened"], c["churn_opened"]) == (2, 2)
    assert c["total_usd"] == 4.0 and c["usd_per_merged_churn_pr"] == 4.0 and c["runs"] == {"workers": 1, "ticks": 1, "agy": 1}
    assert (c["issues_created"], c["issues_closed"], c["issues_net"]) == (5, 7, -2)
    assert c["red_main_hours"] == 6.0 and c["statuses"] == {"done": 1, "crashed": 1}
    assert (c["queue"], c["queue_owner"]) == (2, 1)
    md = churn.scorecard_md(c)
    assert "burning down" in md and "$4.00 per merged churn PR" in md and "1 crashed" in md


def test_scorecard_is_written_once_a_day_on_top_of_the_log(churn, monkeypatch):
    monkeypatch.setattr(churn, "scorecard", lambda day, state: {
        "day": str(day), "merged": [], "churn_merged": [], "opened": 0, "churn_opened": 0,
        "spend": {"workers": 0, "ticks": 0}, "runs": {"workers": 0, "ticks": 0, "agy": 0}, "total_usd": 0,
        "usd_per_merged_churn_pr": None, "issues_created": 0, "issues_closed": 0, "issues_net": 0, "red_main_hours": 0,
        "red_main": [], "statuses": {}, "agy_trial": {"PASS": 0, "FIXED": 0, "REJECT": 0}, "queue": 0, "queue_owner": 0,
        "stalled": [], "quarantined": 0})
    churn.LOG.write_text("# Churn log\n- old line\n")
    assert churn.write_scorecard_if_due() is True
    assert churn.write_scorecard_if_due() is False
    text = churn.LOG.read_text()
    assert text.startswith("# Scorecard ") and text.count("# Scorecard") == 1 and text.endswith("- old line\n")


def test_a_scorecard_failure_never_stops_the_gate(churn, monkeypatch):
    monkeypatch.setattr(churn, "scorecard", lambda day, state: (_ for _ in ()).throw(RuntimeError("gh down")))
    assert churn.write_scorecard_if_due() is False
    assert "scorecard for" in churn.LOG.read_text()


@pytest.mark.parametrize("tool, payload, want", [
    ("RunCommand", '{"CommandLine":"gcc -Wall -c x.c","Cwd":"/w"}', "RunCommand: gcc -Wall -c x.c"),
    ("ReadUrlContent", 'see https://json-schema.org/x {"Url":"https://www.ibm.com/docs/a?b=c","toolAction":1}',
     "ReadUrlContent: https://www.ibm.com/docs/a?b=c"),
])
def test_a_denied_agy_run_names_the_refused_call(churn, tmp_path, monkeypatch, tool, payload, want):
    import sqlite3
    conv = "944e2ff1-d222-4be9-8ed1-8cdd3cf66287"
    monkeypatch.setattr(churn, "AGY_CONV", tmp_path)
    con = sqlite3.connect(tmp_path / f"{conv}.db")
    con.execute("CREATE TABLE steps (id INTEGER, data BLOB)")
    con.execute("INSERT INTO steps VALUES (1, ?)", (payload.encode(),))
    con.commit()
    con.close()
    rd = tmp_path / "run"
    rd.mkdir()
    (rd / "agy.log").write_text(f'tool_confirmation_manager.go:212] Print mode: soft-denying tool confirmation "{tool}" at step 80\n'
                                f"server.go:2513] Tool confirmation for conversation {conv} step 80 (approved=false)\n")
    assert churn.agy_denied_call(rd) == want


def test_owner_holds_do_not_block_launches_and_merge_labels_come_back(churn, monkeypatch):
    """2026-10-14: three PRs on hold for the owner + one with drifted labels held every launch for ~3 h."""
    monkeypatch.setattr(churn, "pace_allows", lambda s, m: (True, "pace off"))
    labels = []
    monkeypatch.setattr(churn, "gh_label", lambda n, add=None, remove=None: labels.append((n, add, remove)))
    h = "aaaa11112222"
    st = {"queue": [item(10)], "running": [], "max_workers": 3,
          "prs": [{"number": n, "reviewed_head": h, "verdict": "hold"} for n in (1, 2, 3)]
          + [{"number": 4, "reviewed_head": h, "verdict": "merge"}, {"number": 5, "reviewed_head": "old", "verdict": "merge"}]}
    snap = {**{str(n): pr(head=h, labels=[]) for n in (1, 2, 3)}, "4": pr(head=h, labels=["shepherd:reapprove"]),
            "5": pr(head=h, labels=["shepherd:reapprove"])}
    assert churn.owner_holds(st, snap) == ["1", "2", "3"]
    assert churn.prs_needing_work(snap, [], st) == ["5"]  # 5 changed since its review: a real need
    reasons, _ = churn.gate(st, {"verdict": "GREEN", "free_slots": 3}, [], {}, snap)
    assert any("#10 (" in r for r in reasons)
    assert churn.fix_merge_labels(st, snap) == ["#4"] and labels == [(4, "shepherd:merge", "shepherd:reapprove")]


# ---------------------------------------------------------------- CI pressure load balancer (owner 2026-10-14)
class TestCiLoadBalancer:
    def test_mode_has_hysteresis(self, churn):
        st = {}
        assert churn.ci_mode(st, {"queued": 31, "median_wait_min": 24})[0] == "busy"
        assert churn.ci_mode({}, {"queued": 21, "median_wait_min": 0.2})[0] == "calm"  # a push's burst, not a jam
        assert churn.ci_mode({}, {"queued": 21, "median_wait_min": 6})[0] == "busy"
        st["ci_mode"] = "busy"
        assert churn.ci_mode(st, {"queued": 12, "median_wait_min": 5})[0] == "busy"  # between thresholds: stays
        assert churn.ci_mode(st, {"queued": 8, "median_wait_min": 9})[0] == "calm"
        st["ci_mode"] = "calm"
        assert churn.ci_mode(st, {"queued": 12, "median_wait_min": 5})[0] == "calm"
        assert churn.ci_mode(st, {"queued": 3, "median_wait_min": 25})[0] == "busy"  # long waits alone count
        assert churn.ci_mode({"ci_mode": "busy"}, {"error": "gh down"})[0] == "busy"

    def test_lane_order_follows_the_mode(self, churn):
        st = {"running": [], "queue": [item(1), item(2, lane="backfill"), item(3, lane="ci"), item(4, pr=44)]}
        assert churn.pick_item(st, {}, "calm")[1]["issue"] == 4   # PR fixes first, always
        st["queue"].pop()
        assert churn.pick_item(st, {}, "calm")[1]["issue"] == 3   # then the ci lane, always
        assert churn.pick_item(st, {}, "busy")[1]["issue"] == 3
        st["queue"].pop()
        assert churn.pick_item(st, {}, "calm")[1]["issue"] == 1   # calm: normal work, never backfill
        assert churn.pick_item(st, {}, "busy")[1]["issue"] == 2   # busy: backfill, never normal
        st["queue"] = [item(1)]
        assert churn.pick_item(st, {}, "busy") == (None, None)
        st["queue"] = [item(2, lane="backfill")]
        assert churn.pick_item(st, {}, "calm") == (None, None)

    def test_backfill_is_capped_and_ignores_the_broken_pr_hold(self, churn, monkeypatch):
        monkeypatch.setattr(churn, "pace_allows", lambda s, m: (True, "pace off"))
        snap = {str(n): pr(labels=["shepherd:needs-fix"]) for n in (1, 2, 3)}
        st = {"queue": [item(9, lane="backfill")], "running": [], "max_workers": 3, "ci_mode": "busy"}
        reasons, _ = churn.gate(st, {"verdict": "GREEN", "free_slots": 3}, [], {}, snap)
        assert any("queue[0] #9" in r and "lane backfill" in r for r in reasons)
        st["running"] = [{"run_id": "r", "pid": 5, "lane": "backfill", "issue": 4628}]
        assert churn.pick_item(st, snap, "busy") == (None, None)

    def test_ci_queue_measures_count_and_median_wait(self, churn, monkeypatch):
        t0 = churn.now()
        runs = [{"created_at": churn.iso(t0 - churn.dt.timedelta(minutes=m))} for m in (5, 20, 60)]
        q = churn.ci_queue(gh=lambda path: {"total_count": 31, "workflow_runs": runs})
        assert q["queued"] == 31 and 19 <= q["median_wait_min"] <= 21
        def boom(path):
            raise RuntimeError("rate limited")
        assert "error" in churn.ci_queue(gh=boom)

    def test_lane_is_validated_and_copied_to_the_running_entry(self, churn):
        st = {"queue": [item(1, lane="fast")]}
        churn.normalize_state(st)
        assert st["queue"] == [] and "lane" in st["quarantine"][-1]["why"]
        st = {"queue": [item(1, lane="normal")]}
        churn.normalize_state(st)
        assert "lane" not in st["queue"][0]



def test_a_declared_flake_stops_the_fix_requeue_until_the_head_moves(churn):
    h = "aaaa11112222"
    st = {"prs": [{"number": 5105, "flake_head": h, "flake_at": churn.iso(churn.now())}], "queue": [], "running": []}
    snap = {"5105": pr(head=h, labels=["shepherd:merge", "shepherd:needs-fix"], branch="churn/5104-x")}
    assert churn.queue_pr_fixes(st, snap) == []
    snap["5105"]["head"] = "bbbb22223333"
    assert churn.queue_pr_fixes(st, snap) == ["#5105"]
    st["queue"] = []
    snap["5105"]["head"] = h
    st["prs"][0]["flake_at"] = churn.iso(churn.now() - churn.dt.timedelta(hours=3))
    assert churn.queue_pr_fixes(st, snap) == ["#5105"]


def test_red_main_issues_become_ci_work_once(churn):
    st = {"queue": [item(1)], "running": []}
    reds = [{"number": 5091, "title": "main is red: Full Suite Gate"}, {"number": 5094, "title": "main is red: Evidence Refresh"}]
    assert churn.queue_main_red(st, reds) == ["#5091", "#5094"]
    assert churn.queue_main_red(st, reds) == []
    q = {x["issue"]: x for x in st["queue"]}
    assert q[5091]["lane"] == "ci" and q[5091]["allow_workflows"] and "Full Suite" in q[5091]["task"]
    assert churn.pick_item(st, {}, "calm")[1]["issue"] in (5091, 5094)
    st2 = {"queue": [], "running": [{"run_id": "r", "pid": 3, "issue": 5091}]}
    assert churn.queue_main_red(st2, reds[:1]) == []  # a live worker is on it


def test_an_approved_pr_no_longer_blocks_its_after_items(churn):
    h = "aaaa11112222"
    st = {"prs": [{"number": 5095, "reviewed_head": h, "verdict": "merge"}]}
    q = item(5082, after_prs=[5095])
    assert churn.blocked_by(q, {"5095": pr(head=h, labels=["shepherd:merge"], ms="behind")}, st) == []
    assert churn.blocked_by(q, {"5095": pr(head=h, labels=["shepherd:merge", "shepherd:needs-fix"])}, st) == [5095]
    assert churn.blocked_by(q, {"5095": pr(head="bbbb22223333", labels=["shepherd:merge"])}, st) == [5095]
    assert churn.blocked_by(q, {"5095": pr(head=h, labels=["shepherd:merge"])}) == [5095]  # no state: strict



class TestBackfillPreemption:
    """Owner 2026-10-14: backfill exists only while the pipeline is clogged; it never slows normal churn."""

    def test_backfill_takes_no_claude_slot(self, churn, monkeypatch):
        monkeypatch.setattr(churn, "pace_allows", lambda s, m: (True, "pace off"))
        run = lambda i, lane=None: {"run_id": f"{i}-x", "pid": i, "issue": i, **({"lane": lane} if lane else {})}  # noqa: E731
        st = {"max_workers": 3, "ci_mode": "calm", "queue": [item(10)],
              "running": [run(1), run(2), run(4628, "backfill")]}
        assert len(churn.claude_slot_holders(st)) == 2
        reasons, _ = churn.gate(st, {"verdict": "GREEN", "free_slots": 1}, [], {}, {})
        assert any("#10 (" in r for r in reasons)          # the third Claude slot is free for normal work
        st.update(ci_mode="busy", queue=[item(10), item(11, lane="backfill")], running=[run(1), run(2), run(3)])
        reasons, _ = churn.gate(st, {"verdict": "GREEN", "free_slots": 1}, [], {}, {})
        assert any("#11 (" in r and "lane backfill" in r for r in reasons)  # busy + 3/3 Claude: backfill still fits

    def test_calm_preempts_and_requeues_a_resume(self, churn):
        stopped = []
        st = {"running": [{"run_id": "4628-x", "pid": 7, "issue": 4628, "lane": "backfill", "model": "opus", "task": "L5"},
                          {"run_id": "5091-y", "pid": 8, "issue": 5091}],
              "queue": [item(1), item(2, lane="backfill")]}
        (churn.RUNS / "4628-x").mkdir(parents=True)
        (churn.RUNS / "4628-x" / "meta.json").write_text('{"worktree": "/w/churn-4628", "branch": "churn/4628-x"}')
        assert churn.preempt_backfill(st, "busy", stopped.append) == [] and stopped == []
        notes = churn.preempt_backfill(st, "calm", stopped.append)
        assert stopped == ["churn-4628-x.service"] and notes
        assert [q["issue"] for q in st["queue"]] == [1, 4628, 2]   # resume goes first in the backfill lane
        r = st["queue"][1]
        assert r["lane"] == "backfill" and r["model"] == "opus" and "/w/churn-4628" in r["task"] and "RESUME" in r["task"]
        assert (churn.RUNS / "4628-x" / "preempted").exists()

    def test_a_preempted_run_is_not_a_crash_and_needs_no_review(self, churn):
        rd = churn.RUNS / "4628-x"
        rd.mkdir(parents=True)
        (rd / "meta.json").write_text('{"worktree": "/w", "branch": "b", "issue": 4628}')
        (rd / "exit").write_text("143\n")
        (rd / "preempted").write_text("t\n")
        st = {"running": [{"run_id": "4628-x", "pid": 0, "issue": 4628, "model": "opus", "lane": "backfill"}]}
        done = churn.reap(st)
        assert done[0]["status"] == "preempted" and not st.get("reports_pending") and st["running"] == []



class TestBackfillCapAndDwell:
    """Owner 2026-10-14 17:50Z: uncapped backfill held the box RED 4 h and was preempted/resumed 5 times in 5 h."""

    def ago(self, churn, minutes):
        return churn.iso(churn.now() - dt.timedelta(minutes=minutes))

    def test_backfill_unit_gets_a_hard_cpu_quota(self, churn, monkeypatch):
        calls = []

        class R:
            returncode, stdout, stderr = 0, "4242", ""

        monkeypatch.setattr(churn, "use_systemd", lambda: True)
        monkeypatch.setattr(churn, "run", lambda cmd, **kw: calls.append(cmd) or R())
        assert churn.spawn_detached("churn-x", ["true"], Path("/"), Path("/dev/null"), ["CPUQuota=400%"]) == 4242
        assert calls[0][calls[0].index("CPUQuota=400%") - 1] == "-p"
        calls.clear()
        churn.spawn_detached("churn-y", ["true"], Path("/"), Path("/dev/null"))
        assert not any("CPUQuota" in x for x in calls[0])
        assert churn.ci_cfg({})["backfill_cores"] == 4 and churn.ci_cfg({"ci_pressure": {"backfill_cores": 2}})["backfill_cores"] == 2

    def test_calm_keeps_a_young_backfill_and_preempts_an_old_one(self, churn):
        stopped = []
        (churn.RUNS / "4628-x").mkdir(parents=True)
        (churn.RUNS / "4628-x" / "meta.json").write_text('{"worktree": "/w", "branch": "b"}')
        st = {"running": [{"run_id": "4628-x", "pid": 7, "issue": 4628, "lane": "backfill", "started": self.ago(churn, 10)}],
              "queue": []}
        notes = churn.preempt_backfill(st, "calm", stopped.append)
        assert stopped == [] and "draining" in notes[0] and st["queue"] == []
        st["running"][0]["started"] = self.ago(churn, 50)
        assert churn.preempt_backfill(st, "calm", stopped.append)[0].startswith("preempted") and stopped

    def test_priority_work_still_preempts_a_young_backfill(self, churn):
        stopped = []
        (churn.RUNS / "4628-x").mkdir(parents=True)
        (churn.RUNS / "4628-x" / "meta.json").write_text('{"worktree": "/w", "branch": "b"}')
        st = {"max_workers": 3, "ci_mode": "busy", "queue": [item(5048, pr=5111)],
              "running": [{"run_id": "4628-x", "pid": 7, "issue": 4628, "lane": "backfill", "started": self.ago(churn, 2)}]}
        assert churn.yield_backfill(st, {}, {"verdict": "RED", "free_slots": 0}, stopped.append) and stopped

    def test_backfill_waits_for_a_sustained_busy_spell(self, churn):
        st = {"ci_mode": "busy", "ci_mode_since": self.ago(churn, 5), "running": [], "queue": [item(4628, lane="backfill")]}
        assert churn.pick_item(st, {}, "busy") == (None, None)
        st["ci_mode_since"] = self.ago(churn, 25)
        assert churn.pick_item(st, {}, "busy")[1]["issue"] == 4628
        del st["ci_mode_since"]  # state from before the field existed
        assert churn.pick_item(st, {}, "busy")[1]["issue"] == 4628

    def test_runnable_tasks_counts_threads_in_state_r(self, churn, tmp_path):
        import threading
        cg = tmp_path / "cg" / "sub"
        cg.mkdir(parents=True)
        (cg / "cgroup.threads").write_text(f"{threading.get_native_id()}\n999999999\n")
        assert churn.runnable_tasks(tmp_path / "cg") == 1  # this thread is running; the dead tid is skipped


class TestCrucibleBackfill:
    """Owner 2026-10-14: crucible-repo steps are the preferred backfill; L5 mutation is last."""

    def test_crucible_steps_go_first_and_mutation_last(self, churn):
        st = {"ci_mode": "busy", "running": [],
              "queue": [item(4628, lane="backfill", task="RESUME backfill run ... L5 det-port mutation"),
                        item(5000, lane="backfill", task="research"),
                        item(5118, lane="backfill", repo="estate-crucible", task="S1 era-1 BLL strata")]}
        assert churn.pick_item(st, {}, "busy")[1]["issue"] == 5118
        st["queue"].pop()
        assert churn.pick_item(st, {}, "busy")[1]["issue"] == 5000
        st["queue"].pop()
        assert churn.pick_item(st, {}, "busy")[1]["issue"] == 4628

    def test_repo_is_a_valid_queue_key_and_named_at_launch(self, churn, monkeypatch):
        assert churn._norm_queue(item(4770, repo="cics-crucible", lane="backfill"))["repo"] == "cics-crucible"
        with pytest.raises(ValueError):
            churn._norm_queue(item(1, repo="language-crucible"))
        monkeypatch.setattr(churn, "pace_allows", lambda s, m: (True, "pace off"))
        st = {"max_workers": 3, "ci_mode": "busy", "ci_mode_since": churn.iso(churn.now() - dt.timedelta(hours=1)),
              "running": [], "queue": [item(4770, repo="cics-crucible", lane="backfill")]}
        reasons, _ = churn.gate(st, {"verdict": "GREEN", "free_slots": 1}, [], {}, {})
        assert any("churn worker --repo cics-crucible" in r for r in reasons)

    def test_repo_of_an_issue_only_when_its_items_agree(self, churn):
        put(churn, queue=[item(5118, repo="estate-crucible", lane="backfill"), item(5118, repo="estate-crucible")])
        assert churn.crucible_repo_of(5118) == "estate-crucible"
        put(churn, queue=[item(4770, repo="cics-crucible", lane="backfill"), item(4770)])
        assert churn.crucible_repo_of(4770) is None


def test_after_merge_items_wait_for_the_merge_not_the_approval(churn):
    """Owner 2026-10-14: #5133 (after #5140) and #5143 (after #5149) were offered every tick once their PRs were
    approved; the tick refused them (they branch off main after the merge) and the starved backfill never launched."""
    v = pr(head="aaaa11112222", labels=["shepherd:merge"])
    st = {"prs": [{"number": 5140, "reviewed_head": "aaaa11112222", "verdict": "merge"}]}
    snap = {"5140": v}
    plain, strict = item(5133, after_prs=[5140]), item(5133, after_prs=[5140], after_merge=True)
    assert churn.blocked_by(plain, snap, st) == [] and churn.blocked_by(strict, snap, st) == [5140]
    assert churn._norm_queue(strict)["after_merge"] is True
    q = {"ci_mode": "busy", "ci_mode_since": churn.iso(churn.now() - dt.timedelta(hours=1)), "running": [],
         "prs": st["prs"], "queue": [strict, item(5118, lane="backfill", repo="estate-crucible")]}
    assert churn.pick_item(q, snap, "busy")[1]["issue"] == 5118  # the blocked ci item no longer starves backfill


def test_stale_gitgalaxy_containers_are_removed(churn):
    old = (churn.now() - dt.timedelta(hours=52)).strftime("%Y-%m-%dT%H:%M:%S.123456789Z")
    new = (churn.now() - dt.timedelta(minutes=20)).strftime("%Y-%m-%dT%H:%M:%S.1Z")
    started = {"a1": old, "b2": new, "c3": old}
    removed = []

    class R:
        def __init__(self, out="", rc=0):
            self.stdout, self.returncode = out, rc

    def docker(*args):
        if args[0] == "ps":
            return R("a1\tgitgalaxy-gnucobol:3\tfervent_matsumoto\nb2\tgitgalaxy-gnucobol:3\tfresh\n"
                     "c3\ticr.io/db2_community/db2\tgitgalaxy-db2\n")
        if args[0] == "inspect":
            return R(started[args[-1]])
        removed.append(args[-1])
        return R()

    notes = churn.reap_containers(4, docker)
    assert removed == ["a1"] and "fervent_matsumoto" in notes[0]


def test_free_slots_are_filled_in_one_tick(churn, monkeypatch):
    monkeypatch.setattr(churn, "pace_allows", lambda s, m: (True, "pace off"))
    st = {"max_workers": 3, "ci_mode": "calm", "running": [],
          "queue": [item(1, lane="ci"), item(2, after_prs=[9]), item(3), item(4, lane="backfill"), item(5)]}
    reasons, _ = churn.gate(st, {"verdict": "GREEN", "free_slots": 3}, [], {}, {"9": pr()})
    r = next(r for r in reasons if r.startswith("launch"))
    assert "launch 3" in r and "queue[0] #1" in r and "queue[2] #3" in r and "queue[4] #5" in r and "#4 (" not in r



def test_red_main_with_a_fix_in_flight_is_not_requeued(churn):
    reds = [{"number": 5091, "title": "main is red: Full Suite"}, {"number": 5094, "title": "main is red: Evidence"}]
    st = {"queue": [], "running": [], "prs": [{"number": 5108, "issue": 5091}],
          "reports_pending": [{"run_id": "5094-x", "issue": 5094, "status": "done", "report": "r"}]}
    assert churn.queue_main_red(st, reds) == []
    st = {"queue": [], "running": [], "prs": [5110]}
    assert churn.queue_main_red(st, reds[:1], {"5110": pr(branch="churn/5091-20261014T1322Z")}) == []



def test_queue_add_before_an_issue(churn, capsys):
    put(churn, queue=[item(1), item(5117), item(5118)])
    ns = argparse.Namespace(action="add", issue=7, task="cics blocker", model="sonnet", needs_owner=False, backend=None,
                            front=False, pr=None, after_pr=None, allow_workflows=False, agy_step=None, lane=None,
                            before_issue=5117, index=None, note=None)
    churn.cmd_queue(ns)
    assert [q["issue"] for q in churn.load_state()["queue"]] == [1, 7, 5117, 5118]



def test_priority_work_evicts_a_box_hogging_backfill(churn):
    stopped = []
    (churn.RUNS / "4628-x").mkdir(parents=True)
    (churn.RUNS / "4628-x" / "meta.json").write_text('{"worktree": "/w", "branch": "b"}')
    st = {"max_workers": 3, "ci_mode": "busy",
          "running": [{"run_id": "4628-x", "pid": 7, "issue": 4628, "lane": "backfill", "model": "opus"}],
          "queue": [item(5048, pr=5111), item(10)]}
    full = {"verdict": "RED", "free_slots": 0}
    assert churn.yield_backfill(st, {}, full, stopped.append) and stopped == ["churn-4628-x.service"]
    stopped.clear()
    st["running"][0].update(pid=8)
    st["queue"] = [item(10)]  # busy mode: normal work waits anyway, so no eviction for it
    assert churn.yield_backfill(st, {}, full, stopped.append) == [] and not stopped
    st["queue"] = [item(5048, pr=5111)]
    assert churn.yield_backfill(st, {}, {"verdict": "GREEN", "free_slots": 1}, stopped.append) == []  # room already


def _picks(churn, st, n):
    """pick_item applied n times, each pick removed: the launch order (pick_items was removed as unused)."""
    q, out = list(st["queue"]), []
    for _ in range(n):
        _, x = churn.pick_item({**st, "queue": q}, {}, "calm")
        if x is None:
            break
        out.append((None, x))
        q.remove(x)
    return out


def test_normal_items_launch_by_tier_then_position(churn):
    st = {"queue": [item(1, task="Siblings of #9 (#1): round 2"), item(2, tier="score"), item(3), item(4, tier="customer"),
                    item(5, tier="era-core"), item(6, tier="era-core")]}
    assert [x["issue"] for _, x in _picks(churn, st, 6)] == [5, 6, 4, 2, 3, 1]
    st["queue"].insert(0, item(7, pr=70, task="Siblings of #9: fix PR"))  # PR fixes and the ci lane still come first
    st["queue"].append(item(8, lane="ci"))
    assert [x["issue"] for _, x in _picks(churn, st, 3)] == [7, 8, 5]


def test_front_only_reorders_within_a_tier(churn):
    put(churn, queue=[item(1, tier="era-core"), item(2, tier="followup")])
    ns = argparse.Namespace(action="add", issue=3, task="Siblings of #2 round 3", model="sonnet", needs_owner=False,
                            backend=None, front=True, pr=None, after_pr=None, allow_workflows=False, agy_step=None,
                            lane=None, before_issue=None, index=None, note=None, tier="followup")
    churn.cmd_queue(ns)
    st = churn.load_state()
    assert [q["issue"] for q in st["queue"]] == [3, 1, 2]
    assert churn.pick_item(st, {}, "calm")[1]["issue"] == 1


def test_queue_add_takes_the_tier_from_the_issue(churn, monkeypatch):
    monkeypatch.setattr(churn, "issue_tier", lambda n, meta=None: {11: "customer"}.get(n))
    put(churn, queue=[])
    for n in (11, 12):
        churn.cmd_queue(argparse.Namespace(action="add", issue=n, task=f"do #{n}", model="sonnet", needs_owner=False,
                                           backend=None, front=False, pr=None, after_pr=None, allow_workflows=False,
                                           agy_step=None, lane=None, before_issue=None, index=None, note=None, tier=None))
    assert [q.get("tier") for q in churn.load_state()["queue"]] == ["customer", None]


@pytest.mark.parametrize("labels,title,want", [
    (["enhancement", "tier:era-core"], "det/CICS: BLL cells", "era-core"),
    (["tier:bogus"], "Siblings of #5079: TD queues", "followup"),
    ([], "Readiness scanner: report", None),
])
def test_issue_tier_reads_the_label(churn, monkeypatch, labels, title, want):
    out = json.dumps({"title": title, "labels": [{"name": n} for n in labels]})
    monkeypatch.setattr(churn, "run", lambda cmd, **kw: argparse.Namespace(returncode=0, stdout=out, stderr=""))
    monkeypatch.setattr(churn, "issue_meta", churn.real_issue_meta)
    assert churn.real_issue_tier(5) == want


def test_bad_tier_is_quarantined(churn):
    put(churn, queue=[item(1, tier="urgent"), item(2, tier="score")])
    st = churn.load_state()
    assert [q["issue"] for q in st["queue"]] == [2] and st["quarantine"]


# ---------------------------------------------------------------- rate pace + opus cap (owner 2026-10-16)
T0 = dt.datetime(2026, 10, 16, 12, 15, tzinfo=dt.timezone.utc)  # a reset: the block runs T0 .. T0+5h
H1 = dt.timedelta(hours=1)


def spend(at, usd, model="legacy"):
    """A ledger entry that counts toward the block but not toward any model's typical cost or rate."""
    return {"t": at.strftime("%Y-%m-%dT%H:%MZ"), "model": model, "cost_usd": usd, "issue": 0}


def rated(churn, monkeypatch, at, queue=(), running=(), log=(), **kw):
    monkeypatch.setattr(churn, "now", lambda: at)
    return {"prs": [], "reports_pending": [], "queue": list(queue), "running": list(running), "max_workers": 2,
            "max_opus": 1, "usage": {"pace": "rate", "learned_budget_usd": 130.0, "log": list(log),
                                     "limit_hits": [{"t": "2026-10-16T09:11Z", "resets_at": churn.iso(T0)}]}, **kw}


def live(n, model, started):
    return {"agent": f"churn r{n}", "run_id": f"r{n}", "pid": 100 + n, "issue": n, "model": model,
            "started": started.strftime("%Y-%m-%dT%H:%MZ")}


def launched(reasons):
    """Queue indexes named by the gate's launch line, in order."""
    line = next((r for r in reasons if r.startswith("launch slot")), "")
    return [int(k) for k in __import__("re").findall(r"queue\[(\d+)\]", line)]


class TestRatePace:
    GREEN = {"verdict": "GREEN", "free_slots": 3}

    def test_a_new_block_starts_at_the_first_use_after_the_last_one_ends(self, churn, monkeypatch):
        st = rated(churn, monkeypatch, T0 + 9 * H1)
        assert churn.block_bounds(st) == (T0 + 9 * H1, T0 + 14 * H1)  # idle since the block ended: one starts now
        st["usage"]["launches"] = [churn.iso(T0 + H1), churn.iso(T0 + 7 * H1)]  # 2026-10-16: churn woke 07:15Z
        assert churn.block_bounds(st) == (T0 + 7 * H1, T0 + 12 * H1)
        assert churn.block_bounds(st, T0 + 4 * H1) == (T0, T0 + 5 * H1)
        assert churn.block_bounds({"usage": {}}) is None

    def test_a_run_that_straddles_the_reset_counts_pro_rata(self, churn, monkeypatch):
        e = {**spend(T0 + H1, 10.0, "sonnet"), "started": churn.iso(T0 - H1)}
        st = rated(churn, monkeypatch, T0 + 2 * H1, log=[e])
        assert churn.block_spend(st, T0, T0 + 2 * H1) == pytest.approx(5.0)

    def test_rates_come_from_timed_runs(self, churn, monkeypatch):
        e = {**spend(T0 + H1, 12.0, "sonnet"), "started": churn.iso(T0 + H1 * 0.5)}  # $12 over 30 min
        st = rated(churn, monkeypatch, T0 + 2 * H1, log=[e])
        assert churn.model_rate(st, "sonnet") == pytest.approx(24.0)
        assert churn.model_rate(st, "opus") == churn.DEFAULT_RATE_USD_H["opus"]

    @pytest.mark.parametrize("at,spent,mode,cap", [
        (T0 + H1, 0, "steady", 1),
        (T0 + H1, 112, "tight", 1),            # room $5 < one sonnet run ($6)
        (T0 + 4 * H1, 30, "surplus", 2),       # 1 h left, $87 would go unused at the reset
        (T0 + 4 * H1, 90, "steady", 1),        # 1 h left but only $27 left: no burst
    ])
    def test_modes(self, churn, monkeypatch, at, spent, mode, cap):
        st = rated(churn, monkeypatch, at, log=[spend(at - H1 * 0.5, spent)] if spent else [])
        plan = churn.rate_plan(st)
        assert (plan["mode"], plan["opus_cap"]) == (mode, cap)

    def test_live_workers_commit_their_remaining_cost(self, churn, monkeypatch):
        st = rated(churn, monkeypatch, T0 + H1, running=[live(1, "opus", T0 + H1 * 0.5)])
        plan = churn.rate_plan(st)
        assert plan["committed"] == pytest.approx(20.0)  # $15 so far at $30/h + $5 more to a typical $20 run
        assert plan["room"] == pytest.approx(117.0 - 20.0) and plan["live_opus"] == 1

    def test_pace_other_than_rate_has_no_plan(self, churn, monkeypatch):
        st = rated(churn, monkeypatch, T0 + H1)
        st["usage"]["pace"] = "off"
        assert churn.rate_plan(st) is None

    def test_opus_cap_one_lets_the_next_sonnet_through(self, churn, monkeypatch):
        st = rated(churn, monkeypatch, T0 + H1, queue=[item(1, model="opus"), item(2, model="opus"), item(3)])
        reasons, _ = churn.gate(st, self.GREEN, [], {}, {})
        assert launched(reasons) == [0, 2]
        assert any("pace rate steady" in r for r in reasons)

    def test_opus_cap_holds_without_rate_pace_too(self, churn, monkeypatch):
        monkeypatch.setattr(churn, "pace_allows", lambda s, m: (True, "pace off"))
        st = rated(churn, monkeypatch, T0 + H1, queue=[item(1, model="opus"), item(2)],
                   running=[live(9, "opus", T0)])
        st["usage"]["pace"] = "off"
        assert launched(churn.gate(st, self.GREEN, [], {}, {})[0]) == [1]

    def test_tight_block_keeps_to_the_cheap_items(self, churn, monkeypatch):
        st = rated(churn, monkeypatch, T0 + H1, queue=[item(1), item(2, model="haiku")], log=[spend(T0, 113)])
        reasons, _ = churn.gate(st, self.GREEN, [], {}, {})
        assert launched(reasons) == [1] and any("pace rate tight" in r for r in reasons)

    def test_room_is_spent_down_pick_by_pick(self, churn, monkeypatch):
        st = rated(churn, monkeypatch, T0 + H1, queue=[item(1), item(2), item(3, model="haiku")], log=[spend(T0, 107)])
        assert launched(churn.gate(st, self.GREEN, [], {}, {})[0]) == [0, 2]  # room $10: sonnet $6, then only haiku fits

    def test_nothing_fits_logs_a_hold(self, churn, monkeypatch):
        st = rated(churn, monkeypatch, T0 + H1, queue=[item(1)], log=[spend(T0, 117)])
        assert launched(churn.gate(st, self.GREEN, [], {}, {})[0]) == []
        assert "launch held by pace: next #1" in churn.QUIET_LOG.read_text()

    def test_surplus_puts_opus_first_and_bursts_to_two(self, churn, monkeypatch):
        st = rated(churn, monkeypatch, T0 + 4 * H1, queue=[item(1), item(2, model="opus"), item(3, model="opus")])
        assert launched(churn.gate(st, self.GREEN, [], {}, {})[0]) == [1, 2]  # two Opus, and no third worker beside them

    def test_surplus_never_jumps_tiers_or_pr_fixes(self, churn, monkeypatch):
        q = [item(1, model="opus", tier="followup"), item(2, tier="era-core"), item(3, pr=77)]
        st = rated(churn, monkeypatch, T0 + 4 * H1, queue=q)
        assert launched(churn.gate(st, self.GREEN, [], {}, {})[0]) == [2, 1]  # the Opus waits: 2 + an Opus is too many

    def test_max_workers_still_caps(self, churn, monkeypatch):
        st = rated(churn, monkeypatch, T0 + 4 * H1, queue=[item(1), item(2), item(3)])
        st["usage"]["rate"] = {"max_workers_burst": 2}
        assert launched(churn.gate(st, self.GREEN, [], {}, {})[0]) == [0, 1]
        st["usage"]["rate"] = {}
        assert launched(churn.gate(st, {"verdict": "GREEN", "free_slots": 1}, [], {}, {})[0]) == [0]  # the box too

    # owner 2026-10-16: 2 Sonnet, sometimes 3, or 1 Sonnet + 1 Opus
    @pytest.mark.parametrize("at,running,queue,want", [
        (T0 + 2 * H1, ["sonnet", "sonnet"], ["sonnet"], [0]),          # $117 over 3 h carries 3 x $10/h: a third
        (T0 + H1, ["sonnet", "sonnet"], ["sonnet"], []),               # 4 h left: $29/h < 3 x $10/h, stay at 2
        (T0 + 2 * H1, ["sonnet", "sonnet"], ["opus"], []),             # never an Opus as the third
        (T0 + 2 * H1, ["opus", "sonnet"], ["haiku"], []),              # an Opus in the set: 2 is the cap
        (T0 + 2 * H1, ["opus"], ["opus", "sonnet"], [1]),              # 1 Opus + 1 Sonnet
        (T0 + 2 * H1, [], ["sonnet", "sonnet", "sonnet"], [0, 1, 2]),
    ])
    def test_worker_mix(self, churn, monkeypatch, at, running, queue, want):
        st = rated(churn, monkeypatch, at, queue=[item(i, model=m) for i, m in enumerate(queue)],
                   running=[live(10 + i, m, at - H1 * 0.2) for i, m in enumerate(running)])
        assert launched(churn.gate(st, self.GREEN, [], {}, {})[0]) == want

    def test_without_rate_pace_max_workers_is_the_cap(self, churn, monkeypatch):
        monkeypatch.setattr(churn, "pace_allows", lambda s, m: (True, "pace off"))
        st = rated(churn, monkeypatch, T0 + 2 * H1, queue=[item(1)], running=[live(10, "sonnet", T0), live(11, "sonnet", T0)])
        st["usage"]["pace"] = "off"
        assert launched(churn.gate(st, self.GREEN, [], {}, {})[0]) == []

    def worker_ns(self, **kw):
        return argparse.Namespace(**{"resume_run": None, "issue": 5, "model": "opus", "task": "t", "brief": "b",
                                     "brief_file": None, "worktree": None, "repo": None, "force": False, "lane": None,
                                     "plan": False, "check_of": None, **kw})

    def test_worker_refuses_a_second_opus(self, churn, monkeypatch, tmp_path):
        monkeypatch.setattr(churn, "make_worktree", lambda i, s: (tmp_path, "b"))
        monkeypatch.setattr(churn, "crucible_repo_of", lambda i: None)
        monkeypatch.setattr(churn, "spawn_detached", lambda *a, **k: 4242)
        put(churn, running=[live(9, "opus", T0)], max_workers=3)
        with pytest.raises(SystemExit, match="Opus worker"):
            churn.cmd_worker(self.worker_ns())
        assert churn.cmd_worker(self.worker_ns(force=True)) == 0

    @pytest.mark.parametrize("status,capped", [("done", False), ("plan-held", True)])
    def test_send_backs_skip_the_cap_but_a_plan_approval_does_not(self, churn, monkeypatch, tmp_path, status, capped):
        monkeypatch.setattr(churn, "spawn_detached", lambda *a, **k: 4242)
        rd = churn.RUNS / "r5"
        rd.mkdir(parents=True)
        (rd / "meta.json").write_text(json.dumps({"run_id": "r5", "issue": 5, "model": "opus", "worktree": str(tmp_path),
                                                  "branch": "b", "session_id": "s", "status": status}))
        put(churn, running=[live(9, "opus", T0)], max_workers=3)
        ns = argparse.Namespace(resume_run="r5", message="fix it", force=False, plan=False)
        if capped:
            with pytest.raises(SystemExit, match="Opus worker"):
                churn.cmd_worker(ns)
        else:
            assert churn.cmd_worker(ns) == 0


class TestConfig:
    def ns(self, key=None, value=None):
        return argparse.Namespace(key=key, value=value, note="test")

    def test_sets_validates_and_logs(self, churn, capsys):
        assert churn.cmd_config(self.ns("pace", "rate")) == 0
        assert churn.cmd_config(self.ns("max_opus", "2")) == 0
        assert churn.cmd_config(self.ns("rate.headroom", "0.85")) == 0
        assert churn.cmd_config(self.ns("block_start", "2026-10-16T12:15Z")) == 0
        st = churn.load_state()
        assert (st["usage"]["pace"], st["max_opus"], st["usage"]["rate"]["headroom"]) == ("rate", 2, 0.85)
        assert st["usage"]["block_start"] == "2026-10-16T12:15Z"
        assert "[owner] config max_opus 1 -> 2 (test)" in churn.LOG.read_text()
        for k, v in [("pace", "fast"), ("max_opus", "9"), ("rate.headroom", "2"), ("nope", "1"), ("block_start", "soon")]:
            with pytest.raises(SystemExit):
                churn.cmd_config(self.ns(k, v))
        capsys.readouterr()
        churn.cmd_config(self.ns())
        assert "max_opus = 2" in capsys.readouterr().out

    def test_bad_max_opus_is_quarantined(self, churn):
        put(churn, max_opus="lots")
        st = churn.load_state()
        assert st["max_opus"] == 1 and st["quarantine"][-1]["where"] == "max_opus"


# ---------------------------------------------------------------- a resumed worker updates its existing PR (#5237)
def _git_wt(tmp_path):
    wt = tmp_path / "wt"
    wt.mkdir()
    for cmd in (["init", "-q", "-b", "churn/4174-20261016T0835Z"], ["commit", "-q", "--allow-empty", "-m", "a"]):
        subprocess.run(["git", "-C", str(wt), "-c", "user.name=t", "-c", "user.email=t@t", *cmd], check=True)
    sha = subprocess.run(["git", "-C", str(wt), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    return wt, sha


def test_worktree_pr_finds_a_pr_pushed_from_the_worktree_under_another_branch_name(churn, tmp_path, monkeypatch):
    wt, sha = _git_wt(tmp_path)
    prs = [{"number": 9, "head": {"ref": "churn/77-other", "sha": sha}},  # another issue: never matched by ancestry
           {"number": 5237, "head": {"ref": "churn/4174-lgipvs01-linked", "sha": sha}}]
    monkeypatch.setattr(churn, "gh_json", lambda path: prs)
    assert churn.real_worktree_pr(str(wt), 4174) == ("churn/4174-20261016T0835Z", 5237)
    prs[1]["head"]["sha"] = "0" * 40  # not this worktree's commit
    assert churn.real_worktree_pr(str(wt), 4174) == ("churn/4174-20261016T0835Z", None)
    prs.append({"number": 7, "head": {"ref": "churn/4174-20261016T0835Z", "sha": "1" * 40}})  # the branch itself
    assert churn.real_worktree_pr(str(wt), 4174)[1] == 7


def test_a_worker_launched_in_a_worktree_with_an_open_pr_is_told_to_update_it(churn, tmp_path, monkeypatch):
    monkeypatch.setattr(churn, "spawn_detached", lambda *a, **k: 4242)
    monkeypatch.setattr(churn, "worktree_pr", lambda wt, issue=None: ("churn/4174-x", 5237))
    monkeypatch.setattr(churn, "gh_json", lambda path: {"head": {"ref": "churn/4174-lgipvs01-linked"}})
    ns = argparse.Namespace(resume_run=None, issue=4174, model="sonnet", task="RESUME", brief="carry on", brief_file=None,
                            worktree=str(tmp_path), branch=None, repo=None, force=True, lane=None, plan=False,
                            check_of=None)
    monkeypatch.setattr(churn, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, "churn/4174-x\n", ""))
    assert churn.cmd_worker(ns) == 0
    prompt = next(churn.RUNS.iterdir()).joinpath("prompt.md").read_text()
    assert prompt.startswith("THIS WORK IS ALREADY OPEN AS PR #5237") and "HEAD:churn/4174-lgipvs01-linked" in prompt
    assert "never rebase" in prompt and prompt.endswith("carry on")


# ---------------------------------------------------------------- CI noise never becomes a PR-fix item (2026-10-16)
SNAP_RED = {"7": {"state": "open", "head": "h7", "branch": "churn/70-x", "labels": ["shepherd:needs-fix"],
                  "mergeable_state": "blocked"},
            "8": {"state": "open", "head": "h8", "branch": "churn/80-x", "labels": [], "mergeable_state": "dirty"}}


@pytest.mark.parametrize("kinds,queued", [(["infra", "main"], False), (["flake", "real"], True), ([], False), (None, True)])
def test_a_red_pr_gets_a_fix_item_only_for_a_real_failure(churn, kinds, queued):
    st = {"prs": [{"number": 7, "issue": 70}], "queue": [], "running": []}
    calls = []
    added = churn.queue_pr_fixes(st, SNAP_RED, triage=lambda n: calls.append(n) or kinds)
    prs = {q["pr"] for q in st["queue"]}
    assert (7 in prs) == queued and 8 in prs and calls == [7]  # a conflict is always a worker's job, no triage
    if kinds == ["infra", "main"]:
        assert st["prs"][0]["flake_head"] == "h7" and "noise:#7 (infra, main)" in added
        assert churn.queue_pr_fixes(st, SNAP_RED, triage=lambda n: 1 / 0) == []  # marked: not re-judged every tick
    if queued:
        assert next(q for q in st["queue"] if q["pr"] == 7)["head"] == "h7"


def test_a_pr_fix_item_is_dropped_once_its_pr_moves_on(churn):
    st = {"queue": [{"issue": 70, "pr": 7, "auto": "pr-fix", "head": "h7"}, {"issue": 80, "pr": 8, "auto": "pr-fix"},
                    {"issue": 90, "pr": 7, "task": "owner item"}]}
    snap = {"7": {"state": "open", "head": "NEW"}, "8": {"state": "open", "head": "h8"}}
    assert churn.prune_pr_fixes(st, snap) == ["#7"]
    assert [q["issue"] for q in st["queue"]] == [80, 90]  # no recorded head: kept; a hand-queued item: kept


# ---------------------------------------------------------------- small re-reviews go to Sonnet (2026-10-16)
def _git(wt, *args):
    return subprocess.run(["git", "-C", str(wt), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                          capture_output=True, text=True, check=True).stdout.strip()


def test_small_delta_counts_only_the_prs_own_code_since_the_reviewed_head(churn, tmp_path):
    wt = tmp_path / "wt"
    wt.mkdir()
    _git(wt, "init", "-q", "-b", "pr")
    (wt / "mod.py").write_text("x = 1\n")
    _git(wt, "add", "-A")
    _git(wt, "commit", "-q", "-m", "base")
    _git(wt, "update-ref", "refs/remotes/origin/main", "HEAD")
    reviewed = _git(wt, "rev-parse", "HEAD")
    _git(wt, "checkout", "-q", "-b", "main-side", "origin/main")  # main moves on: a big change PR didn't write
    (wt / "big.py").write_text("y = 2\n" * 500)
    _git(wt, "add", "-A")
    _git(wt, "commit", "-q", "-m", "main work")
    _git(wt, "update-ref", "refs/remotes/origin/main", "HEAD")
    _git(wt, "checkout", "-q", "pr")
    _git(wt, "merge", "-q", "--no-edit", "origin/main")
    (wt / "tests").mkdir()
    (wt / "tests" / "test_mod.py").write_text("def test_x():\n    assert True\n" * 20)
    (wt / "mod.py").write_text("x = 2\n")
    _git(wt, "add", "-A")
    _git(wt, "commit", "-q", "-m", "fix")
    why = churn.small_delta(str(wt), reviewed)
    assert why and "2 code + 40 test/data lines" in why  # main's 500 lines are not the PR's change
    (wt / "mod.py").write_text("x = 3\n" * 40)
    _git(wt, "commit", "-q", "-am", "big")
    assert churn.small_delta(str(wt), reviewed) is None
    assert churn.small_delta(str(wt), "0" * 40) is None  # an unknown head: Opus


def test_the_tick_runs_on_sonnet_when_every_pending_report_is_a_small_re_review(churn):
    put(churn, reports_pending=[{"run_id": "r1", "issue": 7, "model": "sonnet", "status": "done", "report": "x",
                                 "review_model": "sonnet", "review_why": "small delta since reviewed head ab: 2 code"}],
        workers_paused=True)
    reasons, model = churn.gate(churn.load_state(), {"verdict": "GREEN", "free_slots": 0}, [], {}, {})
    assert model == "sonnet" and "small delta since reviewed head" in reasons[-1]


# ---------------------------------------------------------------- model-free pre-review check (2026-10-16)
GOOD_BODY = "Part of #1.\n\n## Class\n| member | status |\n|---|---|\n| a | handled |\n\n**Sibling gaps**: none, every member handled here." + " ." * 40


def _repo_with_conflict(tmp_path, conflict=True, workflow=False):
    wt = tmp_path / "wt"
    wt.mkdir(parents=True)
    _git(wt, "init", "-q", "-b", "pr")
    (wt / "f.txt").write_text("base\n")
    _git(wt, "add", "-A")
    _git(wt, "commit", "-q", "-m", "base")
    _git(wt, "checkout", "-q", "-b", "m")
    (wt / "f.txt").write_text("main\n" if conflict else "base\n")
    (wt / "g.txt").write_text("main only\n")
    _git(wt, "add", "-A")
    _git(wt, "commit", "-q", "-m", "main")
    _git(wt, "update-ref", "refs/remotes/origin/main", "HEAD")
    _git(wt, "checkout", "-q", "pr")
    (wt / "f.txt").write_text("pr\n" if conflict else "base\n")
    if workflow:
        (wt / ".github" / "workflows").mkdir(parents=True)
        (wt / ".github" / "workflows" / "ci.yml").write_text("on: push\n")
    (wt / "h.txt").write_text("pr work\n")
    _git(wt, "add", "-A")
    _git(wt, "commit", "-q", "-m", "pr")
    return wt


def test_precheck_finds_the_mechanical_send_back_reasons(churn, tmp_path):
    ok = lambda: subprocess.CompletedProcess([], 0, "6/6 gates pass", "")  # noqa: E731
    wt = _repo_with_conflict(tmp_path, conflict=True, workflow=True)
    red = lambda: subprocess.CompletedProcess([], 1, "PASS  lint 0s\nFAIL  audits 5s\n", "")  # noqa: E731
    probs = churn.precheck_problems({"worktree": str(wt), "allow_workflows": False}, "", gates=red)
    assert len(probs) == 4
    assert "conflicts with main in f.txt" in probs[0] and ".github/workflows/ci.yml" in probs[1]
    assert "PR body" in probs[2] and "FAIL  audits" in probs[3]
    clean = _repo_with_conflict(tmp_path / "b", conflict=False, workflow=True)
    assert churn.precheck_problems({"worktree": str(clean), "allow_workflows": True}, GOOD_BODY, gates=ok) == []
    assert churn.precheck_problems({"worktree": str(clean)}, GOOD_BODY, gates=ok) == []  # launched before the flag: unknown


def test_a_failed_precheck_sends_the_run_back_once_then_goes_to_review(churn, monkeypatch):
    sent, nudged = [], []
    monkeypatch.setattr(churn, "gh_json", lambda path: {"body": GOOD_BODY})
    monkeypatch.setattr(churn, "precheck_problems", lambda meta, body: ["pr_gates --fast is red: FAIL  audits"])
    monkeypatch.setattr(churn, "cmd_worker", lambda ns: sent.append(ns) or 0)
    monkeypatch.setattr(churn, "nudge_tick", lambda: nudged.append(1))
    for rid, lineage in (("r1", None), ("r2", "r1")):
        (churn.RUNS / rid).mkdir(parents=True)
        (churn.RUNS / rid / "meta.json").write_text(json.dumps({"run_id": rid, "pr": 7, "worktree": "/x", "precheck_of": lineage}))
    put(churn, reports_pending=[{"run_id": "r1", "issue": 7, "status": "done", "report": "x", "precheck": "pending"},
                                {"run_id": "r2", "issue": 7, "status": "done", "report": "x", "precheck": "pending"}])
    churn.cmd_precheck(argparse.Namespace(run_id="r1"))
    assert [ns.resume_run for ns in sent] == ["r1"] and sent[0].precheck_of == "r1" and "FAIL  audits" in sent[0].message
    assert [e["run_id"] for e in churn.load_state()["reports_pending"]] == ["r2"]  # r1 left for its resumed run
    churn.cmd_precheck(argparse.Namespace(run_id="r2"))  # the resumed run failed again: review, not another loop
    e = churn.load_state()["reports_pending"][0]
    assert len(sent) == 1 and e["precheck"] == "fail" and "FAIL  audits" in e["precheck_problems"] and nudged


def test_a_pending_precheck_holds_its_review_until_the_fuse(churn):
    e = {"run_id": "r1", "issue": 7, "status": "done", "report": "x", "precheck": "pending", "precheck_at": churn.iso(churn.now())}
    put(churn, reports_pending=[e], workers_paused=True)
    assert churn.gate(churn.load_state(), {"verdict": "GREEN", "free_slots": 0}, [], {}, {})[0] == []
    e["precheck_at"] = churn.iso(churn.now() - churn.dt.timedelta(minutes=churn.PRECHECK_FUSE_MIN + 1))
    put(churn, reports_pending=[e], workers_paused=True)
    assert "review: r1" in churn.gate(churn.load_state(), {"verdict": "GREEN", "free_slots": 0}, [], {}, {})[0][0]


# ---------------------------------------------------------------- a declined launch waits without a model (2026-10-16)
def test_a_waiting_item_is_skipped_until_its_run_completes_or_its_time_passes(churn, monkeypatch):
    status = {"s": "in_progress"}
    monkeypatch.setattr(churn, "gh_json", lambda path: {"status": status["s"]})
    put(churn, queue=[item(5133, lane="ci"), item(7)])
    churn.cmd_queue(argparse.Namespace(action="wait", index=0, run=38911788855, minutes=None, note="needs main's artifacts"))
    q = churn.load_state()["queue"]
    assert q[0]["wait_run"] == 38911788855 and churn.waiting(q[0]) == "run 38911788855"
    assert churn.pick_item(churn.load_state(), {}, "calm")[1]["issue"] == 7  # the gate offers the next item instead
    status["s"] = "completed"
    churn._RUN_DONE.clear()  # a new gate pass
    assert churn.waiting(q[0]) is None and churn.pick_item(churn.load_state(), {}, "calm")[1]["issue"] == 5133
    x = {"wait_until": churn.iso(churn.now() + churn.dt.timedelta(minutes=30)), "wait_set": churn.iso(churn.now())}
    assert churn.waiting(x).startswith("until")
    x["wait_set"] = churn.iso(churn.now() - churn.dt.timedelta(hours=churn.WAIT_MAX_H + 1))
    assert churn.waiting(x) is None  # never stranded
    with pytest.raises(SystemExit, match="queue wait needs"):
        churn.cmd_queue(argparse.Namespace(action="wait", index=0, run=None, minutes=None, note="x"))


# ---------------------------------------------------------------- factory metrics (owner 2026-10-16)
def _card():
    return {"day": "2026-10-15", "merged": [], "churn_merged": [], "opened": 0, "churn_opened": 0,
            "spend": {"workers": 0.0, "ticks": 0.0}, "runs": {"workers": 0, "ticks": 0, "agy": 0}, "total_usd": 0.0,
            "usd_per_merged_churn_pr": None, "issues_created": 0, "issues_closed": 0, "issues_net": 0, "red_main_hours": 0,
            "red_main": [], "statuses": {}, "agy_trial": {"PASS": 0, "FIXED": 0, "REJECT": 0}, "queue": 0, "queue_owner": 0,
            "stalled": [], "quarantined": 0, "owner_holds": []}


def test_factory_splits_worker_hours_and_reads_holds_pauses_and_latency(churn, tmp_path):
    runs = tmp_path / "runs"
    def meta(rid, start, end, **kw):
        (runs / rid).mkdir(parents=True)
        (runs / rid / "meta.json").write_text(json.dumps({"run_id": rid, "started": start, "finished": end, "task": "t", **kw}))
    meta("a", "2026-10-15T00:00Z", "2026-10-15T04:00Z")                                  # new work 4h
    meta("b", "2026-10-15T05:00Z", "2026-10-15T06:00Z", task="Fix PR #7: conflicts")       # PR fix 1h
    meta("c", "2026-10-15T07:00Z", "2026-10-15T08:00Z", resume_of="a")                     # send-back 1h
    meta("d", "2026-10-14T23:00Z", "2026-10-15T01:00Z", resume_of="a", precheck_of="a")    # precheck retry, 1h inside the day
    meta("e-agy", "2026-10-15T00:00Z", "2026-10-15T09:00Z", backend="agy")                 # agy: not a Claude slot
    log = "\n".join([
        "- 2026-10-15T01:00Z [churn] reaped a (#1 sonnet): done",
        "- 2026-10-15T01:20Z [churn] review done a: merge",
        "- 2026-10-15T02:00Z [churn] CI mode calm -> busy (x)",
        "- 2026-10-15T03:30Z [churn] CI mode busy -> calm (x)",
        "- 2026-10-15T09:00Z [churn] precheck sent back b (PR #7): conflicts",
        "- 2026-10-15T10:00Z [churn] precheck c (PR #7): pass"])
    quiet = "\n".join([
        "- 2026-10-15T10:00Z [churn] paused until 2026-10-15T12:00Z",
        "- 2026-10-15T10:10Z [churn] paused until 2026-10-15T12:00Z",
        "- 2026-10-15T13:00Z [churn] launch held by pace: next #5",
        "- 2026-10-15T14:00Z [churn] quiet (box GREEN: headroom; 1 running, window $3)",
        "- 2026-10-15T14:10Z [churn] quiet (box GREEN: headroom; 2 running, window $3)"])
    merged = [{"created_at": "2026-10-15T00:00:00Z", "merged_at": "2026-10-15T02:00:00Z"}]
    fa = churn.factory(churn.dt.date(2026, 10, 15), {"max_workers": 2}, log, quiet, merged, runs_dir=runs)
    assert fa["worker_hours"] == {"new_work": 4.0, "pr_fix": 1.0, "send_back": 1.0, "precheck_retry": 1.0}
    assert fa["new_work_share"] == round(4 / 7, 3) and fa["slot_use"] == round(7 / 48, 3)
    assert fa["loops_per_merged_pr"] == 2.0  # b and c started that day; d started the day before
    assert fa["precheck"] == {"sent_back": 1, "passed": 1, "failed_again": 0}
    assert fa["paused_hours"] == 2.0 and fa["ci_busy_hours"] == 1.5 and fa["holds"]["pace"] == 1
    assert fa["idle_ticks"] == 1 and fa["review_wait_min_median"] == 20.0 and fa["pr_open_to_merge_h_median"] == 2.0
    md = churn.scorecard_md({**_card(), "factory": fa})
    assert "Factory: 7.0 worker-hours, 57% new work" in md and "1 idle ticks" in md


def test_factory_history_keeps_30_days_and_publishes_one_file(churn, monkeypatch):
    puts = []
    monkeypatch.setattr(churn, "gh_json", lambda path: {"sha": "abc"})
    monkeypatch.setattr(churn, "run", lambda cmd, **kw: puts.append(cmd) or subprocess.CompletedProcess(cmd, 0, "", ""))
    fa = {"worker_hours": {"new_work": 1.0}, "new_work_share": 1.0}
    for i in range(32):
        day = (churn.dt.date(2026, 9, 1) + churn.dt.timedelta(days=i)).isoformat()
        churn.record_factory({"day": day, "factory": fa, "churn_merged": [1, 2], "total_usd": 9.0,
                              "usd_per_merged_churn_pr": 4.5}, push=(i == 31))
    data = json.loads(churn.FACTORY.read_text())
    assert len(data["days"]) == 30 and min(data["days"]) == "2026-09-03" and data["days"]["2026-10-02"]["churn_merged"] == 2
    assert len(puts) == 1 and puts[0][:4] == ["gh", "api", "-X", "PUT"] and "sha=abc" in puts[0]
    assert "branch=dashboard" in puts[0] and puts[0][4] == f"repos/{churn.DASH_REPO}/contents/factory.json"


def test_a_send_back_at_the_current_head_requeues_its_fix_until_the_head_moves(churn):
    # 2026-10-16: #5204 was sent back, its fix item was dropped as "clean", the head never moved -> 5 items idled ~20 h
    st = {"prs": [{"number": 5204, "issue": 5092, "reviewed_head": "97b44ae8a6f1aa", "verdict": "send-back",
                   "note": "fix the X22 typo"}], "queue": [], "running": []}
    snap = {"5204": pr(head="97b44ae8a6f1", branch="churn/5092-x")}
    assert churn.queue_pr_fixes(st, snap) == ["#5204"]
    q = st["queue"][0]
    assert q["pr"] == 5204 and "review sent it back" in q["task"] and "X22 typo" in q["task"]
    st["queue"] = []
    snap["5204"]["head"] = "0123456789ab"  # pushed: the next review judges the new head
    assert churn.queue_pr_fixes(st, snap) == []


def test_idle_causes_name_why_each_queue_item_could_not_launch(churn):
    st = {"prs": [], "running": [], "queue": [
        {"issue": 1, "after_prs": [5204]}, {"issue": 2, "after_prs": [5204, 5211]}, {"issue": 3, "lane": "backfill"},
        {"issue": 4, "needs_owner": True}, {"issue": 5, "backend": "agy"},
        {"issue": 6, "wait_until": "2999-01-01T00:00Z", "wait_set": churn.iso(churn.now())}]}
    snap = {"5204": {"state": "open", "head": "a"}, "5211": {"state": "closed", "head": "b"}}
    out = churn.idle_causes(st, snap, "calm")
    assert out.startswith("idle: blocked 2 (#5204x2)") and "backfill-calm 1" in out and "owner 1" in out
    assert "agy 1" in out and "wait 1" in out
    assert churn.idle_causes({**st, "queue": [{"issue": 7}]}, snap, "busy") == "idle: ci-busy 1"
    assert churn.idle_causes({**st, "queue": []}, snap, "calm") == "idle: empty"


def test_factory_totals_idle_hours_causes_and_blockers(churn, tmp_path):
    quiet = "\n".join([
        "- 2026-10-15T10:00Z [churn] quiet (box GREEN: headroom; 0 running, window $3) idle: blocked 5 (#5204x5 #5236x2), backfill-calm 9",
        "- 2026-10-15T10:10Z [churn] quiet (box GREEN: headroom; 0 running, window $3) idle: blocked 4 (#5204x4), wait 1",
        "- 2026-10-15T11:30Z [churn] quiet (box GREEN: headroom; 0 running, window $3) idle: empty",
        "- 2026-10-15T12:00Z [churn] quiet (box GREEN: headroom; 0 running, window $3)"])
    fa = churn.factory(churn.dt.date(2026, 10, 15), {"max_workers": 2}, "", quiet, [], runs_dir=tmp_path)
    assert fa["idle_ticks"] == 4 and fa["idle_hours"] == round((10 + 30 + 30 + 30) / 60, 2)
    assert fa["idle_causes"] == {"blocked": 2, "backfill-calm": 1, "wait": 1, "empty": 1}
    assert fa["idle_blockers"] == {"#5204": 2, "#5236": 1}
    md = churn.scorecard_md({**_card(), "factory": {**fa, "worker_hours": {"new_work": 0.0, "pr_fix": 0.0, "send_back": 0.0,
                                                                            "precheck_retry": 0.0}}})
    assert "~1.7h; blocked 2" in md and "blocked behind #5204 2" in md


def _ago(churn, minutes):
    return churn.iso(churn.now() - churn.dt.timedelta(minutes=minutes))


def test_calm_offers_backfill_only_after_the_slots_sat_idle(churn):
    # 2026-10-16: 9 backfill items waited ~6 h of calm idle ticks because backfill was busy-mode only
    snap = {"5204": {"state": "open", "head": "a"}}
    st = {"running": [], "queue": [{"issue": 1, "after_prs": [5204]}, {"issue": 2, "lane": "backfill"}]}
    assert churn.pick_item(st, snap, "calm") == (None, None)  # streak 0: a just-unblocked item may still come
    st["idle_streak"] = 2
    assert churn.pick_item(st, snap, "calm")[1]["issue"] == 2
    assert churn.pick_item({**st, "queue": [{"issue": 3}, *st["queue"]]}, snap, "calm")[1]["issue"] == 3  # normal first
    st["running"] = [{"run_id": "b", "pid": 7, "issue": 9, "lane": "backfill"}]
    assert churn.pick_item(st, snap, "calm") == (None, None)  # backfill_max 1


def test_calm_alone_no_longer_stops_backfill_only_waiting_work_does(churn):
    stopped = []
    (churn.RUNS / "4628-x").mkdir(parents=True)
    (churn.RUNS / "4628-x" / "meta.json").write_text('{"worktree": "/w", "branch": "b"}')
    run = {"run_id": "4628-x", "pid": 7, "issue": 4628, "lane": "backfill", "started": _ago(churn, 60)}
    full = {"verdict": "RED", "free_slots": 0}
    st = {"max_workers": 2, "ci_mode": "calm", "running": [run], "queue": [], "prs": []}
    assert churn.yield_backfill(st, {}, full, stopped.append, mode="calm") == [] and not stopped  # nothing waits
    st["queue"] = [{"issue": 10}]
    assert churn.yield_backfill(st, {}, {"verdict": "GREEN", "free_slots": 1}, stopped.append, mode="calm") == []
    run["started"] = _ago(churn, 10)  # normal work waits the drain window
    assert "draining" in churn.yield_backfill(st, {}, full, stopped.append, mode="calm")[0] and not stopped
    run["started"] = _ago(churn, 60)
    assert churn.yield_backfill(st, {}, full, stopped.append, mode="calm")[0].startswith("preempted") and stopped


def _run(churn, rid, **meta):
    (churn.RUNS / rid).mkdir(parents=True, exist_ok=True)
    (churn.RUNS / rid / "meta.json").write_text(json.dumps({"run_id": rid, **meta}))


def test_a_held_plan_gates_its_requeued_item_without_a_new_plan_run(churn, tmp_path):
    st = {"held_plans": {"5005-a": {"issue": 5005, "t": churn.iso(churn.now()),
                                    "files": ["det/cics.py", "docs/x.md", "tests/tools/equivalence_cics.py"]}},
          "queue": [item(5005, task="requeued: implement plan of run 5005-a")], "running": [], "prs": []}
    assert churn.held_plan(st, item(5005, task="a later slice of #5005")) == (None, None)  # another slice: not its plan
    churn._INFLIGHT = {"PR #5204": {"det/cics.py", "docs/x.md"}, "worker 5005-a": {"tests/tools/equivalence_cics.py"}}
    assert churn.plan_overlap(st, st["queue"][0]) == ["PR #5204: det/cics.py"]  # docs ignored, own run ignored
    assert churn.pick_item(st, {}, "calm") == (None, None)
    assert churn.idle_causes(st, {}, "calm") == "idle: plan-overlap 1"
    churn._INFLIGHT = {"PR #5204": {"docs/x.md"}}
    assert churn.pick_item(st, {}, "calm")[1]["issue"] == 5005
    assert churn.plan_overlap(st, item(5005, pr=7, task="5005-a")) == []
    assert churn.plan_overlap(st, item(5005, lane="backfill", task="5005-a")) == []
    st["held_plans"]["5005-a"]["t"] = churn.iso(churn.now() - churn.dt.timedelta(hours=49))
    churn._INFLIGHT = {"PR #5204": {"det/cics.py"}}
    assert churn.plan_overlap(st, st["queue"][0]) == []  # aged out


def test_direct_spec_covers_pr_fixes_backfill_resumes_and_cleared_held_plans(churn, tmp_path):
    wt = tmp_path / "wt"
    wt.mkdir()
    _run(churn, "5092-a", issue=5092, branch="churn/5092-x", worktree=str(wt), started="2026-10-15T19:07Z")
    _run(churn, "4628-x", issue=4628, branch="b", worktree=str(wt))
    _run(churn, "5005-a", issue=5005, branch="churn/5005-a", worktree=str(wt))
    (churn.RUNS / "5005-a" / "prompt.md").write_text("ORIGINAL BRIEF")
    snap = {"5204": {"state": "open", "branch": "churn/5092-x"}}
    st = {"held_plans": {"5005-a": {"issue": 5005, "t": churn.iso(churn.now()), "files": []}}}
    s = churn.direct_spec(st, snap, item(5092, pr=5204))
    assert s["worktree"] == str(wt) and s["branch"] == "churn/5092-x" and not s["plan"]
    assert churn.direct_spec(st, {"5204": {"branch": "other"}}, item(5092, pr=5204)) is None  # no tree on that branch
    s = churn.direct_spec(st, snap, item(4628, lane="backfill", task="RESUME backfill run 4628-x (preempted ...)"))
    assert s["lane"] == "backfill" and s["worktree"] == str(wt)
    s = churn.direct_spec(st, snap, item(5005, task="implement held run 5005-a"))
    assert s["held_plan"] == "5005-a" and "plan.md" in s["brief"] and "ORIGINAL BRIEF" in s["brief"] and not s["plan"]
    assert churn.direct_spec(st, snap, item(77)) is None  # a new item: the model tick writes its brief


def test_direct_launches_go_in_pick_order_and_stop_at_a_model_item(churn, tmp_path, monkeypatch):
    wt = tmp_path / "wt"
    wt.mkdir()
    _run(churn, "5092-a", issue=5092, branch="churn/5092-x", worktree=str(wt))
    snap = {"5204": {"state": "open", "head": "h", "branch": "churn/5092-x", "labels": ["shepherd:merge"], "mergeable_state": "clean"}}
    put(churn, max_workers=2, queue=[item(5092, pr=5204), item(77), item(5092, pr=5205)], usage={"log": [], "pace": "off"})
    launched = []
    monkeypatch.setattr(churn, "cmd_worker", lambda a: launched.append((a.issue, a.worktree, a.plan)))
    monkeypatch.setattr(churn, "rate_plan", lambda state: None)
    monkeypatch.setattr(churn, "pace_allows", lambda state, m: (True, ""))
    did = churn.direct_launches({"verdict": "GREEN", "free_slots": 3}, snap)
    assert did == ["#5092 (PR fix #5204)"] and launched == [(5092, str(wt), False)]
    assert [q["issue"] for q in churn.load_state()["queue"]] == [77, 5092]  # PR fixes pick first; #5205 has no tree on a branch, so the tick takes it and #77


def test_a_held_plan_approval_is_new_work_not_a_fix_loop(churn):
    _run(churn, "5078-a", status="plan-held")
    _run(churn, "5078-b", status="done")
    assert churn.run_kind({"resume_of": "5078-a"}) == "new_work"
    assert churn.run_kind({"resume_of": "5078-b"}) == "send_back"


def test_a_pr_with_three_recent_fix_runs_goes_to_the_owner(churn):
    for i in range(3):
        _run(churn, f"5176-{i}", issue=5176, task="Fix PR #5180 (b): shepherd:needs-fix", started=churn.iso(churn.now()))
    st = {"prs": [{"number": 5180, "issue": 5176}], "queue": [], "running": []}
    snap = {"5180": pr(ms="dirty", branch="churn/5176-x")}
    assert churn.queue_pr_fixes(st, snap) == ["#5180"]
    q = st["queue"][0]
    assert q["needs_owner"] and "3 fix runs in 24 h" in q["task"]


# ---------------------------------------------------------------- liveness (2026-10-16 audit, step 1)
def test_a_run_queued_for_hours_does_not_hold_ci_busy(churn):
    old = churn.iso(churn.now() - churn.dt.timedelta(hours=5))
    new = churn.iso(churn.now() - churn.dt.timedelta(minutes=2))
    q = churn.ci_queue(lambda path: {"total_count": 3, "workflow_runs": [{"created_at": old}, {"created_at": new},
                                                                          {"created_at": new}]})
    assert q["queued"] == 2 and q["median_wait_min"] < 5 and q["stale"] == 1
    assert churn.ci_mode({"ci_mode": "busy"}, q)[0] == "calm"


def test_a_refused_precheck_send_back_keeps_the_report(churn, monkeypatch):
    monkeypatch.setattr(churn, "gh_json", lambda path: {"body": GOOD_BODY})
    monkeypatch.setattr(churn, "precheck_problems", lambda meta, body: ["PR body lacks Sibling gaps"])
    monkeypatch.setattr(churn, "nudge_tick", lambda: None)

    def refuse(ns):
        raise FileExistsError("run dir exists")
    monkeypatch.setattr(churn, "cmd_worker", refuse)
    (churn.RUNS / "r1").mkdir(parents=True)
    (churn.RUNS / "r1" / "meta.json").write_text(json.dumps({"run_id": "r1", "pr": 7, "worktree": "/x"}))
    put(churn, reports_pending=[{"run_id": "r1", "issue": 7, "status": "done", "report": "x", "precheck": "pending"}])
    churn.cmd_precheck(argparse.Namespace(run_id="r1"))
    e = churn.load_state()["reports_pending"][0]
    assert e["precheck"] == "fail" and "could not launch" in e["precheck_problems"]


def test_reap_watches_the_workers_pr(churn, monkeypatch):
    rd = churn.RUNS / "5092-a"
    rd.mkdir(parents=True)
    (rd / "meta.json").write_text(json.dumps({"worktree": str(rd), "branch": "churn/5092-a", "issue": 5092, "model": "sonnet"}))
    (rd / "exit").write_text("0\n")
    (rd / "out.json").write_text(json.dumps({"result": "done", "session_id": "s"}))
    monkeypatch.setattr(churn, "worktree_pr", lambda wt, issue=None: ("churn/5092-a", 5204))
    st = {"prs": [], "running": [{"run_id": "5092-a", "pid": 0, "issue": 5092, "model": "sonnet"}]}
    churn.reap(st)
    assert st["prs"] == [{"number": 5204, "issue": 5092}]
    churn.reap({**st, "running": [{"run_id": "5092-a", "pid": 0, "issue": 5092, "model": "sonnet"}]})
    assert len(st["prs"]) == 1  # already watched


def test_parked_flaked_and_clean_prs_do_not_hold_normal_churn(churn):
    snap = {str(n): pr(head=f"h{n}", labels=["shepherd:needs-fix"]) for n in (1, 2, 3)}
    st = {"prs": [{"number": 1}, {"number": 2, "flake_head": "h2", "flake_at": churn.iso(churn.now())},
                  {"number": 3, "clean_head": "h3"}], "queue": [], "stalled": []}
    assert churn.prs_needing_work(snap, [], st) == ["1"]
    st["queue"] = [item(9, pr=1, needs_owner=True)]
    assert churn.prs_needing_work(snap, [], st) == []
    st["queue"], st["stalled"] = [], ["orphan PR #1"]
    assert churn.prs_needing_work(snap, [], st) == []


def test_nothing_failing_marks_the_head_clean_and_a_later_failure_queues_a_fix(churn):
    st = {"prs": [{"number": 7, "issue": 3}], "queue": [], "running": []}
    snap = {"7": pr(head="h1", labels=["shepherd:needs-fix"], branch="churn/3-x")}
    assert churn.queue_pr_fixes(st, snap, triage=lambda n: []) == [] and st["prs"][0]["clean_head"] == "h1"
    assert churn.queue_pr_fixes(st, snap, triage=lambda n: ["real"]) == ["#7"]


def test_orphans_name_what_has_no_next_step(churn, monkeypatch):
    monkeypatch.setattr(churn, "pr_is_open", lambda n, snap: True)
    snap = {"10": pr(head="h10", labels=["shepherd:merge"]),            # approved, waits on CI: fine
            "11": pr(head="h11", labels=["shepherd:merge", "shepherd:needs-fix"]),  # red, nothing queued: orphan
            "12": pr(head="h12"),                                         # unreviewed: a review need, fine
            "13": pr(head="h13", labels=["shepherd:merge"])}              # held for the owner: fine
    st = {"prs": [{"number": 10, "reviewed_head": "h10", "verdict": "merge"},
                  {"number": 11, "reviewed_head": "h11", "verdict": "merge"}, {"number": 12},
                  {"number": 13, "reviewed_head": "h13", "verdict": "hold", "note": "workflow edit"}],
          "queue": [item(5, after_prs=[99])], "running": [], "reports_pending": [], "followups": []}
    out = churn.orphans(st, snap, [])
    assert set(out) == {"orphan PR #11", "orphan blocker #99"}
    st["queue"].append(item(5, pr=11))
    assert "orphan PR #11" not in churn.orphans(st, snap, [])
    st["queue"].append(item(6, pr=11, needs_owner=True))
    lines = churn.owner_items(st, snap)
    assert lines[0].startswith("PR #13 on hold: workflow edit") and any("queue #6 (PR #11)" in x for x in lines)


def test_an_owner_workflow_ok_on_the_pr_survives_new_heads(churn, monkeypatch):
    put(churn, prs=[{"number": 5228, "issue": 4725}])
    churn.cmd_pr(argparse.Namespace(action="allow-workflows", number=5228, issue=None, head=None, verdict=None, note=None))
    assert churn.pr_allows_workflows(churn.load_state(), 5228) and not churn.pr_allows_workflows(churn.load_state(), 7)
    seen = []
    monkeypatch.setattr(churn, "gh_json", lambda path: {"body": GOOD_BODY})
    monkeypatch.setattr(churn, "precheck_problems", lambda meta, body: seen.append(meta.get("allow_workflows")) or [])
    monkeypatch.setattr(churn, "nudge_tick", lambda: None)
    (churn.RUNS / "r1").mkdir(parents=True)
    (churn.RUNS / "r1" / "meta.json").write_text(json.dumps({"run_id": "r1", "pr": 5228, "allow_workflows": False}))
    churn.cmd_precheck(argparse.Namespace(run_id="r1"))
    assert seen == [True]


# ---------------------------------------------------------------- scope (owner 2026-10-16, rollout step 3)
def test_scope_orders_normal_work_and_holds_frontier_unless_promoted(churn):
    st = {"running": [], "queue": [item(1, scope="infra"), item(2, scope="frontier"), item(3, scope="sweep"),
                                   item(4, scope="core", tier="score"), item(5)]}
    assert [x["issue"] for _, x in _picks(churn, st, 9)] == [4, 3, 5, 1]  # frontier never
    st["queue"][1]["promoted"] = True
    assert [x["issue"] for _, x in _picks(churn, st, 9)] == [4, 2, 3, 5, 1]  # promoted: core rank, then tier
    assert churn.idle_causes({"queue": [item(2, scope="frontier")], "running": []}, {}, "calm") == "idle: frontier 1"


def test_in_flight_work_is_never_held_by_scope(churn):
    for x in (item(7, scope="frontier", pr=70), item(7, scope="frontier", lane="ci"),
              item(7, scope="frontier", lane="backfill", task="RESUME backfill run 7-a (preempted ...)")):
        assert not churn.scope_held(x)
    assert churn.scope_held(item(7, scope="frontier", lane="backfill"))


def test_queue_add_and_retier_read_the_scope_label(churn, monkeypatch):
    labels = {11: ["scope:core", "tier:customer"], 12: ["scope:frontier"]}
    monkeypatch.setattr(churn, "issue_meta", lambda n: {"title": "t", "labels": [{"name": x} for x in labels.get(n, [])]})
    monkeypatch.setattr(churn, "issue_tier", churn.real_issue_tier)
    base = dict(action="add", task="do it", model="sonnet", front=False, needs_owner=False, backend=None, pr=None,
                after_pr=None, allow_workflows=False, agy_step=None, lane=None, repo=None, before_issue=None, tier=None,
                after_merge=False, index=None, note=None, run=None, minutes=None, promote=False)
    churn.cmd_queue(argparse.Namespace(**{**base, "issue": 11}))
    churn.cmd_queue(argparse.Namespace(**{**base, "issue": 12, "promote": True}))
    q = churn.load_state()["queue"]
    assert (q[0]["scope"], q[0]["tier"]) == ("core", "customer") and q[1]["scope"] == "frontier" and q[1]["promoted"]
    labels[11] = ["scope:sweep"]
    churn.cmd_queue(argparse.Namespace(**{**base, "action": "retier", "issue": None}))
    assert churn.load_state()["queue"][0]["scope"] == "sweep"


def test_unknown_queue_keys_pass_through_and_quarantine_can_be_restored(churn):
    # 2026-10-16: a process on the previous code quarantined every item with the new `scope` key, emptying the queue
    st = {"queue": [item(1, colour="red")]}
    churn.normalize_state(st)
    assert st["queue"][0]["colour"] == "red" and not st.get("quarantine")
    st = {"queue": [item(2)], "quarantine": [
        {"t": "x", "where": "queue[1]", "why": "unknown keys", "value": item(3, scope="core")},
        {"t": "x", "where": "queue[2]", "why": "dup", "value": item(2)},
        {"t": "x", "where": "queue[3]", "why": "bad", "value": "not a dict"},
        {"t": "x", "where": "usage.log", "why": "not a list", "value": 7}]}
    notes = churn.restore_quarantine(st)
    assert [q["issue"] for q in st["queue"]] == [2, 3] and st["queue"][1]["scope"] == "core"
    assert any("already present" in n for n in notes)
    assert [e["where"] for e in st["quarantine"]] == ["queue[3]", "usage.log"]


def test_fresh_items_launch_from_the_skill_brief_only_with_plan_first(churn, tmp_path, monkeypatch):
    skill = tmp_path / "SKILL.md"
    skill.write_text("# x\n## Worker brief (fill in)\n- First: read the worker doc.\n- Draft PR only.\n\n## status\nother\n")
    monkeypatch.setattr(churn, "SKILL_MD", skill)
    st = {"features": {"plan_first": True}, "running": []}
    s = churn.direct_spec(st, {}, item(77, task="Owner OK (2026-10-16): tag it. Do #77", allow_workflows=True))
    assert s["worktree"] is None and s["plan"] is None
    assert "Issue #77" in s["brief"] and "Draft PR only." in s["brief"] and "## status" not in s["brief"]
    assert "## Owner decisions`: Owner OK (2026-10-16): tag it. Do #77" in s["brief"] and "allow_workflows" in s["brief"]
    assert churn.direct_spec({"features": {}}, {}, item(77)) is None  # no plan phase: the tick checks overlap
    assert churn.direct_spec(st, {}, item(77, repo="cics-crucible")) is None
    assert churn.direct_spec(st, {}, item(77, needs_owner=True)) is None
    assert churn.direct_spec(st, {}, item(77, lane="backfill")) is None
    assert churn.direct_spec(st, {"9": {"state": "open", "branch": "churn/77-x"}}, item(77)) is None
    assert churn.direct_spec({**st, "running": [{"issue": 77, "pid": 5}]}, {}, item(77)) is None
    monkeypatch.setattr(churn, "SKILL_MD", tmp_path / "missing.md")
    assert churn.direct_spec(st, {}, item(77)) is None


def test_ticks_run_on_sonnet_unless_an_opus_workers_pr_waits(churn):
    b = {"verdict": "RED", "free_slots": 0}
    def model(pending):
        return churn.gate({"reports_pending": pending, "running": [], "queue": [], "workers_paused": True}, b, [])[1]
    assert model([{"run_id": "1-a", "issue": 1, "model": "sonnet", "status": "done", "pr": 9}]) == "sonnet"
    assert model([{"run_id": "1-a", "issue": 1, "model": "opus", "status": "limit"}]) == "sonnet"
    assert model([{"run_id": "1-a", "issue": 1, "model": "opus", "status": "done", "pr": 9}]) == "opus"
    assert model([{"run_id": "1-a", "issue": 1, "model": "opus", "status": "done", "pr": 9, "review_model": "sonnet"}]) == "sonnet"


def _iss(n, *labels, title="t"):
    return {"number": n, "title": title, "labels": [{"name": x} for x in labels], "milestone": None}


def test_refill_stage1_queues_burned_scopes_in_order_and_skips_the_rest(churn, tmp_path, monkeypatch):
    monkeypatch.setitem(churn.REFILL_DEFAULT, "enabled", True)
    monkeypatch.setitem(churn.REFILL_DEFAULT, "batch", 5)
    issues = [_iss(1, "scope:infra", "bug"), _iss(2, "scope:sweep"), _iss(3, "scope:core", "priority: low"),
              _iss(4, "scope:core", "bug"), _iss(5, "scope:core", "priority: high"), _iss(6, "scope:frontier"),
              _iss(7), _iss(8, "scope:core", "epic"), _iss(9, "scope:infra", title="Call-graph reference: Go"),
              _iss(10, "scope:core"), _iss(11, "scope:core"), _iss(12, "scope:core"), _iss(13, "scope:core")]
    monkeypatch.setattr(churn, "open_issues", lambda: issues)
    monkeypatch.setattr(churn, "issues_with_open_prs", lambda: {10})
    _run(churn, "11-a", issue=11, branch="b", worktree="/x", started=churn.iso(churn.now()))  # worked just now
    _run(churn, "13-a", issue=13, branch="b", worktree="/x", repo="cics-crucible")  # a crucible step: the tick's job
    put(churn, queue=[item(12)], max_workers=2)
    stage, items = churn.refill(churn.load_state(), {})
    assert stage == "rules" and [x["issue"] for x in items] == [4, 5, 3, 2, 1]
    st = churn.load_state()
    assert [x["issue"] for x in st["queue"]] == [12, 4, 5, 3, 2, 1] and st["queue"][1]["auto"] == "refill"
    assert st["queue"][1]["scope"] == "core" and "next unchecked item" in st["queue"][1]["task"]
    assert churn.refill(churn.load_state(), {})[0] == "full"  # 6 launchable >= low-water 4


def test_refill_asks_the_tick_then_falls_back_to_callgraph(churn, monkeypatch):
    monkeypatch.setitem(churn.REFILL_DEFAULT, "enabled", True)
    cg = [_iss(3782, "scope:infra", title="Call-graph reference: Go"), _iss(6, "scope:frontier")]
    monkeypatch.setattr(churn, "open_issues", lambda: cg)
    put(churn, queue=[], max_workers=2)
    assert churn.refill(churn.load_state(), {}) == ("ask", [])  # nothing for the rules: stage 2
    put(churn, queue=[], max_workers=2, refill={"agent_t": churn.iso(churn.now())})
    assert churn.refill(churn.load_state(), {}) == ("wait", [])  # the asked tick gets its grace hour
    put(churn, queue=[], max_workers=2, refill={"agent_t": churn.iso(churn.now() - churn.dt.timedelta(minutes=20))})
    stage, items = churn.refill(churn.load_state(), {})
    assert stage == "callgraph" and [x["issue"] for x in items] == [3782]
    monkeypatch.setattr(churn, "open_issues", lambda: [])  # every call-graph child taken: the standing epic item
    put(churn, queue=[], max_workers=2, refill={"agent_t": churn.iso(churn.now() - churn.dt.timedelta(minutes=20))})
    stage, items = churn.refill(churn.load_state(), {})
    assert [x["issue"] for x in items] == [churn.CALLGRAPH_EPIC] and "call-graph function work" in items[0]["task"]
    assert churn.refill(churn.load_state(), {})[1] == []  # never a second epic item while one is queued


def test_refill_asks_every_30_min_only_while_nothing_is_launchable(churn, monkeypatch):  # owner 2026-10-17
    monkeypatch.setitem(churn.REFILL_DEFAULT, "enabled", True)
    monkeypatch.setattr(churn, "open_issues", lambda: [_iss(3782, "scope:infra", title="Call-graph reference: Go")])
    ago = lambda **kw: {"agent_t": churn.iso(churn.now() - churn.dt.timedelta(**kw))}  # noqa: E731
    put(churn, queue=[], max_workers=2, refill=ago(minutes=40))
    assert churn.refill(churn.load_state(), {}) == ("ask", [])  # idle slots: no 6 h wait
    put(churn, queue=[], max_workers=2, refill=ago(minutes=10))
    assert churn.refill(churn.load_state(), {}) == ("wait", [])  # a 15 min grace for the asked tick
    item = {"issue": 9, "model": "sonnet", "task": "x", "needs_owner": False}
    put(churn, queue=[item], max_workers=2, refill=ago(minutes=40))
    assert churn.refill(churn.load_state(), {})[0] == "wait"  # something launchable: the hourly grace as before


def test_refill_respects_off_paused_and_the_cooldown_log(churn, monkeypatch):
    monkeypatch.setitem(churn.REFILL_DEFAULT, "enabled", True)
    monkeypatch.setattr(churn, "open_issues", lambda: [_iss(4, "scope:core")])
    put(churn, queue=[], workers_paused=True)
    assert churn.refill(churn.load_state(), {})[0] == "off"
    put(churn, queue=[], refill={"enabled": False})
    assert churn.refill(churn.load_state(), {})[0] == "off"
    put(churn, queue=[], refill={"issued": {"4": churn.iso(churn.now())}, "agent_t": churn.iso(churn.now())})
    assert churn.refill(churn.load_state(), {}) == ("wait", [])  # #4 was refilled recently: not again
    assert "label unlabelled" in churn.refill_need({"max_workers": 2})


# --- worktree gc (owner 2026-10-17) -----------------------------------------------------------------------------------

def test_gc_removes_only_finished_unused_worktrees_and_keeps_dirty_ones(churn, tmp_path, monkeypatch):
    wt = tmp_path / "wts"
    monkeypatch.setattr(churn, "WT", wt)
    monkeypatch.setattr(churn, "TOOLS", wt / "churn-tools")
    old, fresh = churn.now() - churn.dt.timedelta(hours=72), churn.now() - churn.dt.timedelta(hours=2)
    trees = [(str(wt / n), b) for n, b in [("done", "churn/1-a"), ("dirty", "churn/2-a"), ("recent", "churn/3-a"),
                                           ("open", "churn/4-a"), ("busy", "churn/5-a"), ("churn-tools", "churn/6-a"),
                                           ("_shared-scratch", "churn/7-a"), ("detached", "")]]
    trees.append(("/elsewhere/done", "churn/8-a"))
    closed = {"churn/1-a": old, "churn/2-a": old, "churn/3-a": fresh, "churn/5-a": old, "churn/6-a": old,
              "churn/7-a": old, "churn/8-a": old}
    _run(churn, "5-x", worktree=str(wt / "busy"))
    st = {"running": [{"run_id": "5-x", "pid": 1}], "queue": [], "reports_pending": []}
    removed = []

    class R:
        def __init__(self, rc):
            self.returncode = rc

    def remove(path):
        removed.append(Path(path).name)
        return R(1 if path.endswith("dirty") else 0)

    monkeypatch.setattr(churn, "run", lambda *a, **k: R(0))  # the prune
    notes = churn.gc_worktrees(st, worktrees=trees, closed=closed, remove=remove)
    assert sorted(removed) == ["dirty", "done"]
    assert notes[0].startswith("removed 1 finished worktree") and "done" in notes[0]
    assert "kept 1" in notes[1] and "dirty" in notes[1]


def test_gc_dry_run_lists_and_the_limit_leaves_the_rest(churn, tmp_path, monkeypatch):
    monkeypatch.setattr(churn, "WT", tmp_path)
    old = churn.now() - churn.dt.timedelta(hours=72)
    trees = [(str(tmp_path / f"t{i}"), f"b{i}") for i in range(5)]
    closed = {f"b{i}": old for i in range(5)}
    calls = []
    notes = churn.gc_worktrees({"queue": []}, dry_run=True, limit=2, worktrees=trees, closed=closed,
                               remove=lambda p: calls.append(p))
    assert not calls and len([n for n in notes if n.startswith("would remove")]) == 2 and notes[-1].startswith("3 more")


def test_gc_runs_every_six_hours_and_not_on_red(churn, monkeypatch):
    monkeypatch.setitem(churn.WT_GC_DEFAULT, "enabled", True)
    calls = []
    monkeypatch.setattr(churn, "gc_worktrees", lambda st: calls.append(1) or ["removed 1"])
    assert churn.gc_worktrees_if_due({"verdict": "RED"}) == [] and not calls
    assert churn.gc_worktrees_if_due({"verdict": "GREEN"}) == ["removed 1"]
    assert churn.gc_worktrees_if_due({"verdict": "GREEN"}) == [] and len(calls) == 1  # within every_h


def test_closed_branches_drops_a_branch_with_an_open_pr(churn, monkeypatch):
    out = json.dumps([{"headRefName": "a", "state": "MERGED", "closedAt": "2026-10-10T00:00:00Z"},
                      {"headRefName": "b", "state": "CLOSED", "closedAt": "2026-10-10T00:00:00Z"},
                      {"headRefName": "b", "state": "OPEN", "closedAt": None}])

    class R:
        returncode, stdout, stderr = 0, out, ""

    monkeypatch.setattr(churn, "run", lambda *a, **k: R())
    assert set(churn.closed_branches()) == {"a"}


# --- phone alerts via ntfy (owner 2026-10-17) -------------------------------------------------------------------------

def _topic(churn, **kw):
    churn.NOTIFY.write_text(json.dumps({"topic": "churn-test", **kw}))


def test_notify_sends_once_per_cooldown_and_never_without_a_topic(churn):
    sent = []
    assert not churn.notify("k", "t", "m", post=sent.append) and not sent  # no topic file: silent
    _topic(churn)
    assert churn.notify("k", "churn: t \u2014 x", "m", priority="urgent", post=sent.append)
    assert sent[0].full_url == "https://ntfy.sh/churn-test" and sent[0].headers["Priority"] == "urgent"
    assert sent[0].headers["Title"] == "churn: t ? x"  # HTTP headers are latin-1
    assert not churn.notify("k", "t", "m", post=sent.append) and len(sent) == 1
    assert churn.notify("other", "t", "m", post=sent.append) and len(sent) == 2


def test_notify_failure_never_raises(churn):
    _topic(churn)

    def boom(req):
        raise churn.urllib.error.URLError("offline")

    assert churn.notify("k", "t", "m", post=boom) is False
    assert "sent" not in churn.notify_cfg()  # not recorded, so the next tick retries


def test_two_failed_ticks_in_a_row_page_and_a_good_tick_resets(churn, monkeypatch):
    pages = []
    monkeypatch.setattr(churn, "notify", lambda key, title, msg, **kw: pages.append(key) or True)

    def bad():
        raise ValueError("state broke")

    for _ in range(2):
        with pytest.raises(ValueError):
            churn.guarded_tick(bad)
    assert pages == ["gate-crash"] and not churn.HEARTBEAT.exists()
    assert churn.guarded_tick(lambda: 0) == 0 and churn.HEARTBEAT.exists() and not churn.GATE_FAILS.exists()


def test_claude_login_failure_pages(churn, monkeypatch):
    pages = []
    monkeypatch.setattr(churn, "notify", lambda key, title, msg, **kw: pages.append((key, kw["priority"])) or True)
    churn.auth_alert("API Error: 401 Invalid bearer token. Please run /login", "worker 1-x")
    churn.auth_alert("ordinary failure: tests red", "worker 2-x")
    assert pages == [("claude-auth", "urgent")]


def test_gate_alerts_disk_red_and_owner_waits_past_twelve_hours(churn, monkeypatch):
    _topic(churn)
    pages = []
    monkeypatch.setattr(churn, "notify", lambda key, title, msg, **kw: pages.append(key) or True)
    st = {"queue": [item(7, needs_owner=True)], "prs": []}
    monkeypatch.setattr(churn, "owner_holds", lambda s, snap: [])
    churn.gate_alerts(st, {"reasons": ["disk almost full, /: 20 GB free of 1900 GB (1%)"]}, {})
    assert pages == ["disk-red"]
    cfg = churn.notify_cfg()
    cfg["owner_seen"]["queue #7"] = churn.iso(churn.now() - churn.dt.timedelta(hours=13))
    churn.NOTIFY.write_text(json.dumps(cfg))
    churn.gate_alerts(st, {"reasons": []}, {})
    assert pages == ["disk-red", "owner:queue #7"]
    churn.gate_alerts({"queue": [], "prs": []}, {"reasons": []}, {})  # answered: forgotten
    assert churn.notify_cfg()["owner_seen"] == {}


def test_watchdog_flags_a_stale_gate_a_dead_timer_and_gh_auth_but_not_a_stop(churn):
    import os
    churn.HEARTBEAT.touch()
    assert churn.watchdog_checks(gh_ok=True, timer_active=True) == []
    old = churn.time.time() - 2 * 3600
    os.utime(churn.HEARTBEAT, (old, old))
    keys = [k for k, _, _ in churn.watchdog_checks(gh_ok=False, timer_active=False)]
    assert keys == ["stale", "timer", "gh-auth"]
    churn.STOP.touch()
    assert churn.watchdog_checks(gh_ok=False, timer_active=False) == []


def test_boot_alert_names_workers_lost_to_the_reboot(churn, monkeypatch):
    put(churn, running=[{"run_id": "5060-x", "pid": 1}])
    sent = []
    monkeypatch.setattr(churn, "notify", lambda key, title, msg, **kw: sent.append((title, msg)) or True)
    assert churn.cmd_notify(argparse.Namespace(action="boot")) == 0
    assert sent[0][0] == "churn: box rebooted" and "5060-x" in sent[0][1]


# --- internet outage (owner 2026-10-17) -------------------------------------------------------------------------------

def _offline_run(churn, rid, issue, **meta):
    _run(churn, rid, issue=issue, worktree=f"/nonexistent/churn-{issue}", branch=f"churn/{issue}-x", **meta)
    rd = churn.RUNS / rid
    (rd / "out.json").write_text(json.dumps({"is_error": True, "terminal_reason": "api_error",
                                             "result": "API Error: Can't reach the API server (ENOTFOUND)"}))
    (rd / "exit").write_text("1")


def test_a_worker_that_lost_the_network_is_requeued_at_the_front_without_a_review(churn):
    _offline_run(churn, "77-x", 77)
    st = {"running": [{"run_id": "77-x", "issue": 77, "model": "sonnet", "task": "fix it", "lane": "ci"}],
          "queue": [item(1)], "reports_pending": [], "prs": [], "usage": {"log": []}}
    done = churn.reap(st)
    assert done[0]["status"] == "offline" and st["reports_pending"] == [] and st["running"] == []
    q = st["queue"][0]
    assert q["issue"] == 77 and q["lane"] == "ci" and q["task"].startswith("RESUME run 77-x") and "fix it" in q["task"]
    assert st["offline"]["requeued"] == 1 and st["queue"][1]["issue"] == 1


def test_an_offline_tick_holds_everything_and_recovery_pages_once(churn, monkeypatch):
    monkeypatch.setattr(churn, "unreachable", lambda hosts=churn.NET_HOSTS: ["api.github.com"])
    monkeypatch.setattr(churn, "refresh_tools", lambda: (_ for _ in ()).throw(AssertionError("no work offline")))
    a = argparse.Namespace(dry_run=False, force=False, reason=[])
    assert churn.cmd_tick(a) == 0 and churn.cmd_tick(a) == 0
    off = churn.load_state()["offline"]
    assert off["ticks"] == 2 and off["since"]
    with churn.edit_state() as st:
        st["offline"]["since"] = churn.iso(churn.now() - churn.dt.timedelta(hours=2))
        st["offline"]["requeued"] = 3
    pages = []
    monkeypatch.setattr(churn, "notify", lambda key, title, msg, **kw: pages.append((title, msg)) or True)
    churn.back_online()
    assert "offline" not in churn.load_state()
    assert pages[0][0] == "churn: back online" and "2.0 h" in pages[0][1] and "3 worker" in pages[0][1]


def test_a_short_blip_is_logged_not_paged(churn, monkeypatch):
    with churn.edit_state() as st:
        st["offline"] = {"since": churn.iso(churn.now() - churn.dt.timedelta(minutes=5)), "ticks": 1}
    pages = []
    monkeypatch.setattr(churn, "notify", lambda *a, **kw: pages.append(a) or True)
    churn.back_online()
    assert not pages and "back online after " in churn.LOG.read_text()  # iso() keeps minutes


def test_urgent_alerts_that_fail_offline_are_delivered_later(churn, monkeypatch):
    _topic(churn)

    def boom(req):
        raise churn.urllib.error.URLError("offline")

    assert not churn.notify("disk-red", "churn: disk almost full", "m", priority="urgent", post=boom)
    assert not churn.notify("disk-red", "churn: disk almost full", "m", priority="urgent", post=boom)  # deduped
    assert not churn.notify("low", "t", "m", post=boom)  # default priority: dropped
    assert [m["key"] for m in churn.notify_cfg()["outbox"]] == ["disk-red"]
    sent = []

    class Resp:
        def close(self):
            pass

    monkeypatch.setattr(churn.urllib.request, "urlopen", lambda req, timeout: sent.append(req) or Resp())
    assert churn.flush_outbox() == 1 and churn.notify_cfg()["outbox"] == []
    assert sent[0].headers["Title"].startswith("churn: disk almost full (delayed, raised ")


def test_watchdog_skips_the_gh_login_check_while_offline(churn, monkeypatch):
    monkeypatch.setattr(churn, "unreachable", lambda hosts=churn.NET_HOSTS: list(hosts))
    assert churn.watchdog_checks(timer_active=True) == []


def test_unreachable_reports_hosts_whose_connect_fails(churn, monkeypatch):
    def connect(addr, timeout):
        if addr[0] == "api.github.com":
            raise OSError("ENOTFOUND")
        return type("S", (), {"close": lambda self: None})()

    monkeypatch.setattr(churn.socket, "create_connection", connect)
    assert churn.real_unreachable() == ["api.github.com"]


# --- weekly limits and failure streaks (owner 2026-10-17) -------------------------------------------------------------

def test_limit_reset_reads_a_dated_weekly_reset(churn):
    local = churn.dt.datetime.now().astimezone()
    day = local + churn.dt.timedelta(days=4)
    r = churn.limit_reset(f"You've hit your weekly limit · resets {day.strftime('%b')} {day.day}, 9pm (Europe/Berlin)")
    got = r.astimezone()
    assert (got.month, got.day, got.hour, got.minute) == (day.month, day.day, 21, 5)
    wd = local + churn.dt.timedelta(days=3)
    got = churn.limit_reset(f"weekly limit reached, resets {wd.strftime('%A')} at 9:30am").astimezone()
    assert (got.day, got.hour, got.minute) == (wd.day, 9, 35)
    soon = churn.limit_reset("You've hit your session limit · resets 11:59pm")  # the 5-hour form still works
    assert churn.dt.timedelta(0) < soon - churn.now() <= churn.dt.timedelta(hours=24, minutes=5)
    assert churn.limit_reset("tests failed") is None


def test_a_long_limit_pause_and_a_limit_loop_page(churn, monkeypatch):
    pages = []
    monkeypatch.setattr(churn, "notify", lambda key, title, msg, **kw: pages.append(key) or True)
    st = {"usage": {"log": []}}
    churn.record_limit(st, churn.now() + churn.dt.timedelta(days=3), "tick", "weekly limit · resets Oct 21, 9pm")
    assert pages[0].startswith("long-limit-") and st["paused_until"]
    st = {"usage": {"log": [], "limit_hits": [{"t": churn.iso(churn.now() - churn.dt.timedelta(hours=h)), "resets_at": "x"}
                                              for h in (1, 2, 3)]}}
    churn.record_limit(st, churn.now() + churn.dt.timedelta(hours=1), "tick")
    assert "limit-loop" in pages


def test_four_failed_workers_in_a_row_page_and_hold_launches(churn, monkeypatch):
    pages = []
    monkeypatch.setattr(churn, "notify", lambda key, title, msg, **kw: pages.append((key, msg)) or True)
    monkeypatch.setattr(churn, "claude_version_note", lambda: "Claude Code 9.9.9.")
    st = {}
    for i, status in enumerate(["error", "crashed", "limit", "offline", "error"]):  # limit/offline do not count
        churn.track_failures(st, status, f"r{i}", "boom")
    assert st["fail_streak"]["n"] == 3 and not pages and "paused_until" not in st
    churn.track_failures(st, "error", "r9", "Unknown option --permission-mode")
    assert pages[0][0] == "fail-streak" and "9.9.9" in pages[0][1] and "--permission-mode" in pages[0][1]
    assert churn.parse_iso(st["paused_until"]) > churn.now()
    churn.track_failures(st, "done", "r10", "")
    assert "fail_streak" not in st


def test_claude_version_note_names_the_version_and_a_recent_install(churn, tmp_path, monkeypatch):
    v = tmp_path / "versions" / "2.1.302"
    v.parent.mkdir()
    v.write_text("")
    monkeypatch.setattr(churn, "CLAUDE", str(v))
    note = churn.claude_version_note()
    assert note.startswith("Claude Code 2.1.302, installed ") and "recent update" in note


def test_host_settings_come_from_the_config_file(tmp_path, monkeypatch):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"project_root": "/data/proj", "repo": "acme/widget", "python": "/opt/venv/bin/python"}))
    monkeypatch.setenv("PROMOTER_BOT_CONFIG", str(cfg))
    monkeypatch.delenv("CHURN_HOME", raising=False)
    loader = importlib.machinery.SourceFileLoader("churn_cfg", str(SCRIPT))
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader("churn_cfg", loader))
    loader.exec_module(mod)
    assert (mod.REPO, mod.ORG, str(mod.H), str(mod.V6), str(mod.WT)) == (
        "acme/widget", "acme", "/data/proj/handoffs", "/data/proj/repo", "/data/proj-worktrees")
    assert mod.DASH_REPO == "acme/squid-bot" and mod.SELFSCAN_PY == "/opt/venv/bin/python"
    brief = mod.AGY_BRIEF.format(issue=1, wt="w", branch="b", task="t", repo=mod.REPO, repo_dir=mod.V6, python=mod.SELFSCAN_PY)
    assert "/data/proj/repo" in brief and "/opt/venv/bin/python -m pytest" in brief


def _procs(loop_age=55, target=False):
    loop = "/bin/bash -c source snap.sh && eval 'until ! pgrep -f \"evidence.py prove\" >/dev/null; do sleep 15; done'"
    p = {100: (1, "python3 churn _run 5053-x", 300), 101: (100, "/usr/bin/claude -p go", 300),
         102: (101, loop, loop_age), 103: (102, "sleep 15", 0.1)}
    if target:
        p[200] = (1, "python3 tools/evidence.py prove", 10)
    return p


def test_stuck_workers_flags_a_wait_loop_that_matches_only_itself(churn):
    st = {"running": [{"run_id": "5053-x", "pid": 100}]}
    [(key, _, msg)] = churn.stuck_workers(st, _procs(), transcript=lambda pid: None)
    assert key == "stuck-5053-x" and "kill 102" in msg
    assert churn.stuck_workers(st, _procs(target=True), transcript=lambda pid: None) == []  # really waiting
    assert churn.stuck_workers(st, _procs(loop_age=5), transcript=lambda pid: None) == []  # too young to judge
    assert churn.stuck_workers({"running": [{"pid": 100}]}, _procs(), transcript=lambda pid: None) == []  # agy/legacy


def test_stuck_workers_flags_a_silent_transcript(churn, tmp_path):
    import os
    t = tmp_path / "s.jsonl"
    t.write_text("{}")
    st = {"running": [{"run_id": "5053-x", "pid": 100}]}
    procs = _procs(target=True)
    assert churn.stuck_workers(st, procs, transcript=lambda pid: t) == []
    old = churn.time.time() - 50 * 60
    os.utime(t, (old, old))
    [(_, _, msg)] = churn.stuck_workers(st, procs, transcript=lambda pid: t)
    assert "50 min" in msg and "evidence.py prove" in msg


def test_gate_ends_a_dead_wait_loop_on_its_second_sighting_only(churn, monkeypatch):
    st = {"running": [{"run_id": "5053-x", "pid": 100}]}
    killed, clock = [], [1_000_000.0]
    monkeypatch.setattr(churn.time, "time", lambda: clock[0])
    end = lambda procs: churn.end_dead_wait_loops(st, procs, kill=lambda pid, sig: killed.append(pid))
    assert end(_procs(loop_age=12)) == [] and killed == []  # first sighting: remembered
    clock[0] += 9 * 60
    [line] = end(_procs(loop_age=21))
    assert killed == [102] and "5053-x" in line and "evidence.py prove" in line
    assert end(_procs(loop_age=5)) == []  # too young
    killed.clear()
    end(_procs(loop_age=12))
    clock[0] += 9 * 60
    assert end(_procs(loop_age=21, target=True)) == [] and killed == []  # the job came back: a real wait


def test_gate_treats_a_reused_pid_as_a_new_loop(churn, monkeypatch):
    st = {"running": [{"run_id": "5053-x", "pid": 100}]}
    killed, clock = [], [1_000_000.0]
    monkeypatch.setattr(churn.time, "time", lambda: clock[0])
    churn.end_dead_wait_loops(st, _procs(loop_age=12), kill=lambda pid, sig: killed.append(pid))
    clock[0] += 9 * 60
    churn.end_dead_wait_loops(st, _procs(loop_age=11), kill=lambda pid, sig: killed.append(pid))  # started 8 min later
    assert killed == []


def test_every_churn_session_gets_the_self_wait_hook(churn):
    cmd = churn.claude_cmd("go", "sonnet", 1.0)
    hooks = json.loads(cmd[cmd.index("--settings") + 1])["hooks"]["PreToolUse"]
    assert hooks[0]["matcher"] == "Bash" and str(churn.SELF_WAIT_HOOK) in hooks[0]["hooks"][0]["command"]
    assert churn.SELF_WAIT_HOOK.exists()


@pytest.mark.parametrize(("cmd", "blocked"), [
    ('until ! pgrep -f "evidence.py prove" >/dev/null; do sleep 15; done; tail ev.log', True),
    ('while pgrep -f "det_mutation.py run" >/dev/null; do sleep 30; done', True),
    ("while pgrep -af det_mutation.py; do sleep 30; done", True),
    ('while ps aux | grep -q "heavy-run"; do sleep 5; done', True),
    ('until ! pgrep -f "[e]vidence.py prove" >/dev/null; do sleep 15; done', False),
    ("while kill -0 $pid 2>/dev/null; do sleep 15; done", False),
    ('pgrep -af "evidence.py prove"', False),
    ("while pgrep -x java >/dev/null; do sleep 5; done", False),
    ('until ! pgrep -f "evidence.py prove"; do sleep 15; done # self-wait-ok', False),
])
def test_self_wait_hook_blocks_only_loops_that_match_themselves(cmd, blocked):
    hook = SCRIPT.parent / "tools" / "block-self-wait.py"
    out = subprocess.run(["python3", str(hook)], input=json.dumps({"tool_input": {"command": cmd}}),
                         capture_output=True, text=True, check=True).stdout
    assert ('"deny"' in out) is blocked


class _BotGH:
    """A fake GitHub for tend_bot_prs: one bot PR, its merge state, its workflow runs."""

    def __init__(self, state="blocked", age_h=2, runs=(), busy=False, head="aaa111"):
        self.state, self.runs, self.busy, self.head = state, list(runs), busy, head
        self.created = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=age_h)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.calls = []

    def gh(self, path):
        if path.startswith("repos/o/r/pulls?"):
            return [{"number": 5317, "head": {"ref": "auto/evidence-refresh-full", "sha": self.head}, "created_at": self.created},
                    {"number": 9, "head": {"ref": "churn/1-x", "sha": "b"}, "created_at": self.created}]
        if path == "repos/o/r/pulls/5317":
            return {"mergeable_state": self.state}
        if "/workflows/" in path:
            return {"workflow_runs": [{"id": 1}] if self.busy else []}
        return {"workflow_runs": self.runs}

    def sh(self, cmd):
        self.calls.append(cmd[:3])
        return subprocess.CompletedProcess(cmd, 0)


@pytest.fixture
def bot(churn, monkeypatch):
    monkeypatch.setattr(churn, "REPO", "o/r")
    monkeypatch.setitem(churn._CFG, "bot_prs", {"auto/evidence-refresh": "evidence-refresh.yml"})
    pages = []
    monkeypatch.setattr(churn, "notify", lambda key, *a, **k: pages.append(key))
    return churn, pages


def test_bot_pr_that_conflicts_is_rebuilt_once_per_head(bot):
    churn, _ = bot
    g = _BotGH(state="dirty")
    [line] = churn.tend_bot_prs(g.gh, g.sh)
    assert "#5317 conflicts with main" in line and g.calls == [["gh", "workflow", "run"]]
    assert churn.tend_bot_prs(g.gh, g.sh) == [] and len(g.calls) == 1  # dispatched: wait for the new head
    g.head = "bbb222"
    churn.tend_bot_prs(g.gh, g.sh)
    assert len(g.calls) == 2  # a new head that conflicts again gets its own rebuild


def test_bot_pr_rebuild_waits_for_a_running_refresh_and_old_prs_are_rebuilt(bot):
    churn, _ = bot
    g = _BotGH(state="dirty", busy=True)
    assert churn.tend_bot_prs(g.gh, g.sh) == [] and g.calls == []
    g = _BotGH(age_h=30)
    [line] = churn.tend_bot_prs(g.gh, g.sh)
    assert "open 30 h" in line


def test_bot_pr_red_check_is_rerun_once_then_pages(bot):
    churn, pages = bot
    g = _BotGH(runs=[{"id": 7, "name": "ground-truth", "conclusion": "failure", "run_attempt": 1},
                     {"id": 8, "name": "lint", "conclusion": "success"}])
    [line] = churn.tend_bot_prs(g.gh, g.sh)
    assert "reran" in line and g.calls == [["gh", "run", "rerun"]]
    assert churn.tend_bot_prs(g.gh, g.sh) == [] and pages == []  # the rerun has not finished
    g.runs[0]["run_attempt"] = 2
    [line] = churn.tend_bot_prs(g.gh, g.sh)
    assert "paged" in line and pages == ["bot-pr-5317-aaa111"] and len(g.calls) == 1
    assert churn.tend_bot_prs(g.gh, g.sh) == []  # paged once per head


def test_bot_prs_off_without_config_and_churn_prs_are_ignored(bot, monkeypatch):
    churn, _ = bot
    g = _BotGH(state="dirty")
    monkeypatch.setitem(churn._CFG, "bot_prs", {})
    assert churn.tend_bot_prs(g.gh, g.sh) == [] and g.calls == []
