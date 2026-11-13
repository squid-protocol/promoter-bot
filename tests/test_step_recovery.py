"""Tests for the step journal hook and the takeover of a dead run.

In its own file so that branches adding tests do not all append to test_churn.py and conflict with each other.
"""

from __future__ import annotations

import datetime as dt  # noqa: F401
import json
import subprocess  # noqa: F401
from pathlib import Path  # noqa: F401

import pytest  # noqa: F401
from test_churn import _git, _run, churn  # noqa: F401  (the fixture and the shared helpers)


# ---------------------------------------------------------------- step-level recovery (2026-11-13)

def _journal_hook():
    import importlib.util as u
    spec = u.spec_from_file_location("step_journal", Path(__file__).resolve().parents[1] / "tools" / "step-journal.py")
    mod = u.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _hook_call(cmd, stdout="", ms=1000, cwd="/nonexistent", **resp):
    return {"tool_name": "Bash", "tool_input": {"command": cmd}, "duration_ms": ms, "cwd": cwd,
            "tool_response": {"stdout": stdout, "stderr": "", "interrupted": False, **resp}}


def test_the_step_journal_records_finished_milestones_and_claims_nothing_it_cannot_see(tmp_path):
    sj = _journal_hook()
    steps = lambda *a, **k: [(e["step"], e["ok"]) for e in sj.entries(_hook_call(*a, **k))]  # noqa: E731
    assert steps("git commit -m 'x'") == [("commit", True)]
    assert steps("git add -A && git commit -qm x && git push -u origin churn/1-a") == [("commit", True), ("push", True)]
    assert steps("git -C /w merge --no-edit origin/main") == [("merged-main", True)]
    pr = sj.entries(_hook_call("gh pr create --draft --body-file b.md", "https://github.com/o/r/pull/8421\n"))
    assert [(e["step"], e["detail"]) for e in pr] == [("pr", "#8421")]
    # the gates' verdict comes from the output: piped into tail the command succeeds whatever the gates did
    assert steps("python tests/tools/pr_gates.py --fast | tail -3", "6/6 gates pass") == [("gates", True)]
    assert steps("python tests/tools/pr_gates.py --only lint affected 2>&1 | tail", "1/2 gates pass; failing: affected") == [("gates", False)]
    assert steps("python tests/tools/pr_gates.py --fast | tail -1", "PASS lint 3s") == []  # no verdict line: no claim
    gates = sj.entries(_hook_call("tests/tools/box/heavy-run.sh python3 tests/tools/pr_gates.py --only lint affected", "2/2 gates pass"))
    assert gates[0]["args"] == "--only lint affected"
    assert steps("churn selfcheck --body b.md", "selfcheck: 0 problem(s) (pr_gates not run: --gates to include)") == [("selfcheck", True)]
    assert steps("churn selfcheck --body b.md | cat", "- touches 6 files\nselfcheck: 1 problem(s)") == [("selfcheck", False)]
    # a repository tool that ran a minute or more is worth not running twice; a quick one is not recorded
    slow = sj.entries(_hook_call("python tests/tools/proof_sweep.py --det-only --work /tmp/w", ms=11 * 60_000))
    assert [(e["step"], e["detail"]) for e in slow] == [("tool:proof_sweep", "11 min")]
    assert steps("python tests/tools/proof_sweep.py --det-only", ms=4000) == [] and steps("ls -la") == []
    assert steps("git commit -m x", interrupted=True) == []
    # end to end: one line per milestone, with the commit and whether the tree was clean; never an error
    wt = tmp_path / "wt"
    wt.mkdir()
    _git(wt, "init", "-q", "-b", "main")
    (wt / "a.py").write_text("x = 1\n")
    _git(wt, "add", "-A"), _git(wt, "commit", "-q", "-m", "one")
    import os
    import sys
    out = tmp_path / "journal.jsonl"
    hook = Path(__file__).resolve().parents[1] / "tools" / "step-journal.py"
    for payload, env in ((_hook_call("git commit -m one", cwd=str(wt)), {"CHURN_JOURNAL": str(out)}),
                         (_hook_call("git push", cwd=str(wt)), {}), ("not json", {"CHURN_JOURNAL": str(out)})):
        r = subprocess.run([sys.executable, str(hook)], input=payload if isinstance(payload, str) else json.dumps(payload),
                           capture_output=True, text=True, env={"PATH": os.environ["PATH"], **env})
        assert r.returncode == 0 and r.stdout == ""
    (line,) = [json.loads(x) for x in out.read_text().splitlines()]
    assert line["step"] == "commit" and line["clean"] is True and line["head"] == _git(wt, "rev-parse", "--short=12", "HEAD")


def _dead_run(churn, tmp_path, journal=(), dirty=False, pushed=True):
    """A worktree with two commits of its own on churn/7-a, its run dir and journal. Returns (meta, worktree)."""
    origin, wt = tmp_path / "origin.git", tmp_path / "wt"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    subprocess.run(["git", "clone", "-q", str(origin), str(wt)], check=True, capture_output=True)
    _git(wt, "checkout", "-q", "-b", "main")
    (wt / "base.py").write_text("x = 0\n")
    _git(wt, "add", "-A"), _git(wt, "commit", "-q", "-m", "base"), _git(wt, "push", "-q", "origin", "main")
    _git(wt, "checkout", "-q", "-b", "churn/7-a")
    for n in ("one", "two"):
        (wt / f"{n}.py").write_text("x = 1\n")
        _git(wt, "add", "-A"), _git(wt, "commit", "-q", "-m", f"step {n}")
    if pushed:
        _git(wt, "push", "-q", "origin", "churn/7-a")
    if dirty:
        (wt / "two.py").write_text("x = 2\n")
    head = _git(wt, "rev-parse", "--short=12", "HEAD")
    meta = {"run_id": "7-a", "issue": 7, "worktree": str(wt), "branch": "churn/7-a", "status": "crashed",
            "started": "2026-11-13T10:00Z", "finished": "2026-11-13T10:40Z", "task": "slice 3", "model": "sonnet"}
    _run(churn, "7-a", **meta)
    (churn.RUNS / "7-a" / "prompt.md").write_text("THE ORIGINAL BRIEF")
    (churn.RUNS / "7-a" / "journal.jsonl").write_text("".join(
        json.dumps({"t": "2026-11-13T10:30Z", "head": head, "clean": True, "ok": True, **e}) + "\n" for e in journal))
    return meta, wt, head


def test_a_takeover_is_told_what_the_dead_run_finished(churn, tmp_path):
    meta, wt, head = _dead_run(churn, tmp_path, journal=[
        {"step": "merged-main"}, {"step": "gates", "args": "--fast", "ok": False, "head": "0000000", "detail": "5/6 gates pass"},
        {"step": "gates", "args": "--fast", "detail": "6/6 gates pass"}, {"step": "selfcheck", "detail": "selfcheck: 0 problem(s)"},
        {"step": "tool:proof_sweep", "detail": "11 min", "cmd": "python tests/tools/proof_sweep.py --det-only"}])
    (churn.RUNS / "7-a" / "plan.md").write_text("the plan")
    st = churn.run_steps({**meta, "pr": 8421})
    done, todo = "\n".join(st["done"]), "\n".join(st["todo"])
    assert st["head"] == head and st["branch"] == "churn/7-a" and st["journal"] == 5
    assert "the plan is written" in done and "2 commit(s) of this work" in done and "step two; " in done
    assert f"pr_gates --fast passed at this commit ({head})" in done  # the later pass, not the earlier failure
    assert "churn selfcheck passed at this commit" in done and "proof_sweep (11 min" in done
    assert "is pushed at this commit" in done and "PR #8421 is open" in done and todo == ""
    brief = churn.recovery_brief({**meta, "pr": 8421}, st, "crashed", "slice 3")
    assert brief.startswith("RECOVERY of run 7-a: its process ended without a result")
    assert "FINISHED, do not do again:" in brief and "do not run it again unless you change a file" in brief
    assert brief.rstrip().endswith("THE ORIGINAL BRIEF") and "Queue task: slice 3" in brief
    warm = churn.recovery_brief(meta, st, "limit", "slice 3", warm=True)
    assert "a usage limit cut it off" in warm and "THE ORIGINAL BRIEF" not in warm  # its own session still has it


def test_work_in_hand_and_stale_verdicts_are_left_to_do(churn, tmp_path):
    meta, wt, head = _dead_run(churn, tmp_path, dirty=True, pushed=False, journal=[
        {"step": "gates", "args": "--fast", "detail": "6/6 gates pass"}, {"step": "selfcheck", "ok": False, "detail": "selfcheck: 1 problem(s)"}])
    st = churn.run_steps(meta)
    todo = "\n".join(st["todo"])
    assert "1 uncommitted change(s) are in the worktree (two.py)" in todo and "never discard them unread" in todo
    assert "pr_gates --fast passed at" in todo and "run it again once, at the end" in todo  # the tree changed since
    assert "churn selfcheck FAILED" in todo and "is not pushed at this commit (nothing pushed yet)" in todo
    assert "no PR is open for this branch yet" in todo
    assert not any("passed at this commit" in x for x in st["done"])
    # a takeover that dies too: the next one reads the whole chain's journal
    _run(churn, "7-b", **{**meta, "run_id": "7-b", "recovered_from": "7-a"})
    (churn.RUNS / "7-b" / "journal.jsonl").write_text(json.dumps({"step": "push", "ok": True, "head": head, "clean": True}) + "\nnot json\n")
    assert [(e["run"], e["step"]) for e in churn.journal("7-b")] == [("7-a", "gates"), ("7-a", "selfcheck"), ("7-b", "push")]


def test_only_a_dead_run_with_its_worktree_is_taken_over_and_only_twice(churn, tmp_path):
    meta, wt, _ = _dead_run(churn, tmp_path)
    r = {"issue": 7, "model": "opus", "task": "slice 3", "scope": "core"}
    item = churn.recover_item({}, r, meta, "crashed")
    assert item["recover"] == "7-a" and item["model"] == "opus" and item["task"].startswith("RECOVER run 7-a (crashed ")
    assert item["auto"] == "recover" and item["scope"] == "core" and "pr" not in item  # never read as a PR-fix item
    assert churn._norm_queue(dict(item))["recover"] == "7-a"
    for status in ("offline", "limit"):
        assert churn.recover_item({}, r, meta, status)
    for status in ("done", "denied", "error", "budget", "preempted", "plan-held"):
        assert churn.recover_item({}, r, meta, status) is None  # it ended with a result of its own: reviewed as before
    assert churn.recover_item({}, r, {**meta, "recover_depth": churn.RECOVER_MAX}, "crashed") is None  # it keeps dying
    assert churn.recover_item({}, r, {**meta, "worktree": str(tmp_path / "gone")}, "crashed") is None
    assert churn.recover_item({}, {**r, "lane": "experiment"}, meta, "crashed") is None
    assert churn.recover_item({}, r, {**meta, "precheck_of": "7-0"}, "crashed") is None


def test_the_gate_launches_a_takeover_itself_warm_when_the_session_still_is(churn, tmp_path, monkeypatch):
    meta, wt, _ = _dead_run(churn, tmp_path)
    x = {"issue": 7, "model": "sonnet", "task": "RECOVER run 7-a (crashed): slice 3", "recover": "7-a"}
    spec = churn.direct_spec({}, {}, x)
    assert spec["worktree"] == str(wt) and spec["plan"] is False and spec["recover"] == "7-a"
    assert "resume_run" not in spec and "THE ORIGINAL BRIEF" in spec["brief"]  # a crash left no session to resume
    fresh = churn.iso(churn.now() - dt.timedelta(minutes=10))
    _run(churn, "7-a", **{**meta, "status": "limit", "session_id": "s1", "finished": fresh})
    warm = churn.direct_spec({}, {}, x)
    assert warm["resume_run"] == "7-a" and "THE ORIGINAL BRIEF" not in warm["brief"]
    _run(churn, "7-a", **{**meta, "status": "limit", "session_id": "s1", "finished": "2026-11-13T10:40Z"})
    assert "resume_run" not in churn.direct_spec({}, {}, x)  # past the prompt cache: a fresh session, told what is done
    _run(churn, "7-a", **{**meta, "worktree": str(tmp_path / "gone")})
    assert churn.direct_spec({}, {}, x) is None  # nothing to take over: the model tick decides
