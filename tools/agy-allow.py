#!/usr/bin/env python3
"""Add the allow rules agy's headless churn runs were refused on (2026-10-13), after a timestamped backup.

Refused calls seen: gcc (#5055), docker image inspect (#5048), read_url of IBM's CICS docs (#5039).
Adds read-only docker queries, the C/COBOL compilers the CICS stubs need, and IBM's documentation site. Nothing
that writes outside the worktree, pushes, or installs. Run it yourself; it prints what it changed.
"""
import datetime
import json
import os
import shutil

S = os.path.expanduser("~/.gemini/antigravity-cli/settings.json")
ADD = ["command(gcc)", "command(cc)", "command(cobc)", "command(docker image inspect)", "command(docker image ls)",
       "command(docker ps)", "read_url(www.ibm.com)"]

raw = open(S).read()
d = json.loads(raw)
allow = d.setdefault("permissions", {}).setdefault("allow", [])
new = [x for x in ADD if x not in allow]
if not new:
    print("nothing to add; already allowed:", ADD)
    raise SystemExit(0)
backup = f"{S}.bak-{datetime.datetime.now():%Y%m%d-%H%M%S}"
shutil.copy2(S, backup)
allow.extend(new)
tmp = S + ".tmp"
with open(tmp, "w") as fh:
    fh.write(json.dumps(d, indent=2 if '\n  "' in raw else 4) + "\n")
json.load(open(tmp))
os.replace(tmp, S)
os.chmod(S, 0o600)
print("backup:", backup)
print("added:", new)
