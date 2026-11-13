"""Tests for the CI cost by kind of PR, the instructions revision per run and the lesson grader.

In its own file so that branches adding tests do not all append to test_churn.py and conflict with each other.
"""

from __future__ import annotations

import datetime as dt  # noqa: F401
import json
import subprocess  # noqa: F401
from pathlib import Path  # noqa: F401

import pytest  # noqa: F401
from test_churn import _git, _run, churn  # noqa: F401  (the fixture and the shared helpers)


# ---------------------------------------------------------------- learning loops (2026-11-13)

def test_a_pr_and_a_task_are_sorted_into_ci_kinds(churn):
    assert churn.pr_kind(["docs/a.md", "README.md"]) == "docs"
    assert churn.pr_kind(["tests/equivalence/x/case.json", "tests/golden_master_audit/a.json"]) == "data"
    assert churn.pr_kind(["a.py", "docs/a.md"]) == "code" and churn.pr_kind([]) == "code"
    assert churn.pr_kind(["a.py"], "cics-crucible v0.15.0: pin bump (6 cases), re-baseline") == "pin"
    assert churn.pr_kind(["tests/cics_crucible/pin.json", "a.py"]) == "pin"
    prof = {"issue_kind": {"7954": "data"}}
    assert churn.task_kind("Bump the cics-crucible pin to 3ddfd6f", 1, prof) == "pin"
    assert churn.task_kind("Doc drift: qualify four script names", 1, prof) == "docs"
    assert churn.task_kind("#7955 L5 slice: ghost-tasks family. One family per PR, data only.", 1, prof) == "data"
    assert churn.task_kind("next slice", 7954, prof) == "data"  # its issue's last PR was data
    assert churn.task_kind("next slice", 99, prof) is None


def test_pushes_are_priced_by_kind_and_a_launch_by_its_own_kind(churn, monkeypatch):
    monkeypatch.setattr(churn, "pid_alive", lambda pid: True)
    prof = {"workflows": {"smoke.yml": {"runner_min": 30, "n": 9}, "docs.yml": {"runner_min": 2, "n": 9},
                          "det.yml": {"runner_min": 90, "n": 9}}}
    cfg = churn.ci_profile_cfg({})

    def push(sha, pr, branch, *wfs, conclusion="success"):
        return [{"event": "pull_request", "status": "completed", "conclusion": conclusion, "head_sha": sha,
                 "head_branch": branch, "path": f".github/workflows/{w}", "pull_requests": [{"number": pr}]} for w in wfs]

    files = {1: ["docs/a.md"], 2: ["a.py"], 3: ["a.py"], 4: None}
    runs = (push("s1", 1, "churn/11-a", "docs.yml") + push("s2", 2, "churn/22-a", "smoke.yml", "det.yml")
            + push("s3", 3, "fix/x", "smoke.yml") + push("s4", 4, "churn/44-a", "smoke.yml")
            + push("s5", 2, "churn/22-a", "smoke.yml", conclusion="cancelled"))
    assert churn.learn_push_kinds(runs, prof, cfg, {}, files=files.get) == 3  # PR 4's files unknown, s5 superseded
    assert prof["per_push_by_kind"]["docs"] == {"n": 1, "rm": 2.0}
    assert prof["per_push_by_kind"]["code"]["n"] == 2  # 120, then folded with 30
    assert prof["issue_kind"] == {"11": "docs", "22": "code"}
    # too few pushes of a kind seen: every launch keeps the one price
    st = {"ci_profile": {**prof, "per_push_rm": 40, "target_util": 0.5}, "running": [{"run_id": "a", "pid": 1, "task": "t", "issue": 22}],
          "queue": [{"issue": 11, "task": "next slice"}]}
    ad = churn.ci_admission(st, {"queued_rm": 100, "active_rm": 20, "pool": 20})
    assert ad == {"target_util": 0.5, "per_launch_rm": 80, "committed_rm": 80, "room_rm": 400, "admit": 5}
    # enough seen: the live code worker is priced as code, the next (docs) launch as docs
    st["ci_profile"]["per_push_by_kind"] = {"docs": {"n": 9, "rm": 2.0}, "code": {"n": 9, "rm": 100.0}}
    ad = churn.ci_admission(st, {"queued_rm": 100, "active_rm": 20, "pool": 20})
    # 600 - 120 in hand - 200 (code: 100 x 2 pushes) = 280; a docs launch costs 4
    assert ad["committed_rm"] == 200 and ad["room_rm"] == 280 and ad["admit"] == 70
    assert ad["next_kind"] == "docs" and ad["next_launch_rm"] == 4 and ad["per_launch_rm"] == 80
    st["queue"] = [{"issue": 5, "task": "owner ruling", "needs_owner": True}, {"issue": 22, "task": "next slice"}]
    assert churn.ci_admission(st, {"queued_rm": 100, "active_rm": 20, "pool": 20})["admit"] == 1  # 280 // 200
    st["ci_profile"]["per_push_by_kind"]["code"]["rm"] = -5  # never a price that is not a real cost
    assert churn.ci_admission(st, {"queued_rm": 100, "active_rm": 20, "pool": 20})["admit"] == 5


def test_each_run_records_the_instructions_revision_it_read(churn, tmp_path):
    wt = tmp_path / "repo"
    wt.mkdir()
    _git(wt, "init", "-q", "-b", "main")
    (wt / "CLAUDE.md").write_text("one\n")
    _git(wt, "add", "-A"), _git(wt, "commit", "-q", "-m", "rules 1")
    first = _git(wt, "rev-parse", "--short", "HEAD")
    (wt / "code.py").write_text("x = 1\n")
    _git(wt, "add", "-A"), _git(wt, "commit", "-q", "-m", "code only")
    code_head = _git(wt, "rev-parse", "HEAD")
    assert churn.rules_rev(str(wt)).split()[0] == first  # a code commit does not move the revision
    (wt / ".claude" / "rules").mkdir(parents=True)
    (wt / ".claude" / "rules" / "a.md").write_text("two\n")
    _git(wt, "add", "-A"), _git(wt, "commit", "-q", "-m", "rules 2")
    churn._RULES_REV.clear()
    assert churn.rules_rev(str(wt)).split()[0] == _git(wt, "rev-parse", "--short", "HEAD")
    assert churn.rules_rev(str(wt), code_head).split()[0] == first  # an older run is placed by its starting head
    assert churn.rules_rev(str(tmp_path / "nowhere")) is None


def test_the_retro_sees_fix_rates_per_instructions_revision(churn):
    t = churn.now() - dt.timedelta(days=3)

    def at(h):
        return churn.iso(t + dt.timedelta(hours=h))
    _run(churn, "1-a", issue=1, pr=101, started=at(0), rules_rev="aaa 2026-11-01")           # first pass
    _run(churn, "2-a", issue=2, pr=102, started=at(1), rules_rev="aaa 2026-11-01")           # one conflict fix
    _run(churn, "2-b", issue=2, pr=102, started=at(2), task="Fix PR #102: conflicts with main", rules_rev="bbb 2026-11-08")
    (churn.RUNS / "2-b" / "cause.json").write_text(json.dumps({"cause": "conflict"}))
    _run(churn, "3-a", issue=3, pr=103, started=at(5), head_start="deadbeef")                # before revisions were recorded
    _run(churn, "3-b", issue=3, pr=103, started=at(6), resume_of="3-a", precheck_of="3-a")   # a precheck retry is no fix
    _run(churn, "4-a", issue=4, started=at(7), rules_rev="bbb 2026-11-08")                   # no PR: not counted
    _run(churn, "5-a", issue=5, pr=105, started="2026-01-01T00:00Z", rules_rev="old 2026-01-01")
    got = churn.rules_effect(churn.now() - dt.timedelta(days=28), rev_of=lambda sha: "bbb 2026-11-08")
    assert [e["rev"] for e in got] == ["aaa 2026-11-01", "bbb 2026-11-08"]
    assert got[0] == {"rev": "aaa 2026-11-01", "first_run": at(0), "prs": 2, "fixed": 1, "causes": {"conflict": 1}, "fix_rate": 0.5}
    assert got[1]["prs"] == 1 and got[1]["fixed"] == 0 and got[1]["fix_rate"] == 0.0


def test_a_lesson_is_graded_before_the_retro_may_promote_it(churn):
    lessons = [
        "- [ ] A bare `cat > file` with no heredoc in Bash blocks on stdin and hangs; write scratch scripts with the Write tool (PR #8378)",
        "- [ ] be careful (PR #1)",
        "- [ ] golden-lock.sh must wrap crucible_check.py --update or two re-bless runs corrupt the golden master store",
        "- [ ] A bare `cat > file` with no heredoc in Bash blocks on stdin and hangs; write scratch scripts using the Write tool (PR #8401)",
        "- [ ] never rebase a worker branch onto main: merge origin/main instead, rebasing rewrites pushed commits (PR #7000)",
        "- [ ] survivor_attack.py reports every mutant refused when one commarea_hex is empty: read baseline.log first (PR #8380)",
    ]
    rules = {".claude/rules/git.md": "Workers never rebase a worker branch onto main. Merge origin/main instead: rebasing "
                                     "rewrites pushed commits and breaks the review trail."}
    asked = []

    def ask(items):
        asked.extend(items)
        return {"0": {"grade": "keep", "why": "names the command and the fix"}, "5": {"grade": "drop", "why": "one PR's state"}}
    g = churn.grade_lessons(lessons, rules, ask=ask)
    assert [e["grade"] for e in g] == ["keep", "drop", "drop", "drop", "drop", "drop"]
    assert [i["id"] for i in asked] == ["0", "5"]  # only the ones the mechanical checks let through cost a model call
    assert "too short" in g[1]["why"] and "cites no PR" in g[2]["why"] and "repeats an earlier" in g[3]["why"]
    assert g[4]["covered_by"] == ".claude/rules/git.md" and "already in one passage" in g[4]["why"]
    # the same words scattered over a long file are not a rule that says it (2026-11-13: 6 of 10 real lessons were
    # called covered against whole files)
    spread = {"big.md": "\n\n".join(f"Paragraph {i} about {w} and unrelated matters." for i, w in enumerate(
        "never rebase worker branch onto main merge origin instead rebasing rewrites pushed commits".split()))}
    assert "covered_by" not in churn.lesson_checks(lessons[4:5], spread)[0]
    assert g[5]["why"] == "one PR's state" and all("problems" not in e for e in g)
    quiet = churn.grade_lessons(lessons[:1], rules, ask=lambda items: 1 / 0)
    assert quiet[0]["grade"] == "ungraded"  # the grader failed: the retro judges it alone, as before
    assert churn.grade_lessons([], rules, ask=lambda items: pytest.fail("nothing to grade")) == []
