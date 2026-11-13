"""Tests for the separate GitHub identity of worker sessions.

In its own file so that branches adding tests do not all append to test_churn.py and conflict with each other.
"""

from __future__ import annotations

import os
import subprocess

import pytest
from test_churn import churn  # noqa: F401  (the fixture)


def test_no_worker_token_file_changes_nothing(churn):
    assert churn.worker_identity_env("") == {} and churn.worker_identity_env() == {}


def test_a_worker_gets_the_bot_token_for_gh_and_for_git(churn, tmp_path):
    tok = tmp_path / "bot.token"
    tok.write_text("ghp_bot123\n")
    tok.chmod(0o600)
    env = churn.worker_identity_env(str(tok))
    assert env["GH_TOKEN"] == env["GITHUB_TOKEN"] == "ghp_bot123"
    # git really resolves it: ssh remotes are rewritten to https, and the credential comes from `gh`, which reads
    # GH_TOKEN (a stand-in gh here, so no network and no real login)
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "gh").write_text('#!/bin/sh\nif [ "$1 $2 $3" = "auth git-credential get" ]; then '
                             'echo "username=x-access-token"; echo "password=$GH_TOKEN"; fi\n')
    (fake / "gh").chmod(0o755)
    run_env = {"PATH": f"{fake}:{os.environ['PATH']}", "HOME": str(tmp_path), "GIT_CONFIG_NOSYSTEM": "1", **env}
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True, env=run_env)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", "git@github.com:owner/repo.git"], check=True, env=run_env)
    url = subprocess.run(["git", "-C", str(repo), "remote", "get-url", "--push", "origin"], capture_output=True, text=True,
                         env=run_env).stdout.strip()
    assert url == "https://github.com/owner/repo.git"  # never the owner's ssh key
    cred = subprocess.run(["git", "-C", str(repo), "credential", "fill"], input="protocol=https\nhost=github.com\n\n",
                          capture_output=True, text=True, env=run_env).stdout
    assert "password=ghp_bot123" in cred and "username=x-access-token" in cred


@pytest.mark.parametrize("make,why", [
    (lambda f: None, "cannot read"),
    (lambda f: (f.write_text("ghp_x\n"), f.chmod(0o644)), "readable by other users"),
    (lambda f: (f.write_text("\n"), f.chmod(0o600)), "holds no single token"),
    (lambda f: (f.write_text("two tokens\n"), f.chmod(0o600)), "holds no single token"),
])
def test_an_unusable_token_file_fails_the_run_instead_of_using_the_owners_login(churn, tmp_path, make, why):
    f = tmp_path / "bot.token"
    make(f)
    with pytest.raises(RuntimeError, match=why):
        churn.worker_identity_env(str(f))


def test_the_old_command_name_is_a_link_to_the_program_and_both_home_variables_work(tmp_path, monkeypatch):
    """2026-11-13: the program file is `promoter-bot`; `churn` is a link to it, because every installed host's command,
    units and workers call that name and a rename that removed it would stop the gate at its next self-update."""
    import importlib.machinery
    import importlib.util
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    assert (root / "churn").is_symlink() and (root / "churn").resolve() == root / "promoter-bot"
    assert os.access(root / "promoter-bot", os.X_OK)
    monkeypatch.setenv("PROMOTER_BOT_CONFIG", str(tmp_path / "none.json"))
    for var in ("PROMOTER_BOT_HOME", "CHURN_HOME"):
        monkeypatch.delenv("PROMOTER_BOT_HOME", raising=False)
        monkeypatch.delenv("CHURN_HOME", raising=False)
        monkeypatch.setenv(var, str(tmp_path / var))
        loader = importlib.machinery.SourceFileLoader(f"pb_{var}", str(root / "churn"))  # through the old name
        mod = importlib.util.module_from_spec(importlib.util.spec_from_loader(f"pb_{var}", loader))
        loader.exec_module(mod)
        assert mod.H == tmp_path / var and Path(mod.SELF).name == "promoter-bot"
    monkeypatch.setenv("CHURN_HOME", str(tmp_path / "old"))  # both set: the new name wins
    monkeypatch.setenv("PROMOTER_BOT_HOME", str(tmp_path / "new"))
    loader = importlib.machinery.SourceFileLoader("pb_both", str(root / "promoter-bot"))
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader("pb_both", loader))
    loader.exec_module(mod)
    assert mod.H == tmp_path / "new"
