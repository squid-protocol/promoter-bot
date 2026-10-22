#!/usr/bin/env python3
"""Replay ci-snapshots.jsonl logs (#36) under the retired busy/calm rule and under CI admission (#38).

    tools/ci-replay.py ~/gitgalaxy/handoffs/ci-snapshots.jsonl melek-ci-snapshots.jsonl [--target 5] [--since ISO]

Per host: ticks each rule held normal work, and how many of those holds came while PR jobs really waited over the
target (job_wait_p50, the outcome admission steers by). The busy/calm rule is recomputed from each row's queued count
and median wait with its 2026-10-14 thresholds and hysteresis, so rows logged after the switch still compare."""
import argparse
import collections
import json

OLD = {"busy_queued": 20, "busy_wait_min": 20, "calm_queued": 8, "calm_wait_min": 10, "busy_count_min_wait": 5}


def old_rule(prev: str, n: int, w: float) -> str:
    if (n >= OLD["busy_queued"] and w >= OLD["busy_count_min_wait"]) or w >= OLD["busy_wait_min"]:
        return "busy"
    if n <= OLD["calm_queued"] and w < OLD["calm_wait_min"]:
        return "calm"
    return prev


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("logs", nargs="+")
    ap.add_argument("--target", type=float, default=5.0, help="PR job wait target, min (default 5)")
    ap.add_argument("--since", default="", help="only rows at or after this ISO time")
    a = ap.parse_args()
    rows = []
    for path in a.logs:
        with open(path) as fh:
            for ln in fh:
                try:
                    r = json.loads(ln)
                except ValueError:
                    continue
                if isinstance(r, dict) and "admit" in r and "error" not in r and str(r.get("t", "")) >= a.since:
                    rows.append(r)
    by_host = collections.defaultdict(list)
    for r in sorted(rows, key=lambda r: r["t"]):
        by_host[r.get("host", "?")].append(r)
    print(f"target: PR job wait p50 <= {a.target:g} min")
    for host, rs in sorted(by_host.items()):
        mode, c = rs[0].get("mode", "calm"), collections.Counter()
        for r in rs:
            mode = old_rule(mode, int(r.get("queued") or 0), float(r.get("median_wait_min") or 0))
            over = (r.get("job_wait_p50") or 0) > a.target
            new = int(r["admit"]) < 1
            c["over"] += over
            c["old"] += mode == "busy"
            c["old_over"] += mode == "busy" and over
            c["new"] += new
            c["new_over"] += new and over
            c["missed"] += over and not new
        print(f"{host}: {len(rs)} ticks {rs[0]['t']} .. {rs[-1]['t']}; {c['over']} with PR job wait over target")
        print(f"  busy/calm held {c['old']} ticks ({c['old_over']} over target, {c['old'] - c['old_over']} not)")
        print(f"  admission held {c['new']} ticks ({c['new_over']} over target, {c['new'] - c['new_over']} not); "
              f"admitted on {c['missed']} over-target ticks")


if __name__ == "__main__":
    main()
