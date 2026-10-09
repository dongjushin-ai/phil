#!/usr/bin/env python3
"""Which research data actually helps? Brier delta per data source.

Operator-added 2026-10-09. Reads settled rows from journal/forecasts.jsonl
(note field) and journal/ledger.jsonl (rationale field) and groups them by
the "[src:a,b,c]" tag the agent writes when a source informed the estimate
(see strategy/tools/intel.py for the source names: stock, earnings, calendar,
news, social, pmflow, fx — plus any other label the agent chooses, e.g.
odds, kalshi, siblings).

brier_delta = agent Brier - market Brier (same sign as core/score.py):
negative = the agent beat the market price on those rows. A source earns
weight in the playbook only when its delta is negative over enough rows
(n >= 20 is the default bar for a "verdict"); "untagged" is the baseline.

Usage:
  python3 strategy/tools/source_score.py            # table
  python3 strategy/tools/source_score.py --json     # machine-readable
  python3 strategy/tools/source_score.py --min-n 10 # lower verdict bar
"""
import argparse
import json
import pathlib
import re
from collections import defaultdict

ROOT = pathlib.Path(__file__).resolve().parents[2]
TAG = re.compile(r"\[src:([a-z0-9_,\- ]+)\]", re.I)


def _rows(path, text_field, kind):
    p = ROOT / path
    if not p.exists():
        return []
    out = []
    for line in p.open():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if r.get("status") not in ("won", "lost") or r.get("superseded_by"):
            continue
        p_agent = r.get("est_prob")
        p_mkt = r.get("market_prob_at_record", r.get("market_prob_at_entry"))
        if p_agent is None or p_mkt is None:
            continue
        won = 1.0 if r["status"] == "won" else 0.0  # same rule as core/score.py
        m = TAG.search(r.get(text_field) or "")
        srcs = sorted({s.strip().lower() for s in m.group(1).split(",") if s.strip()}) if m else []
        out.append({"kind": kind, "srcs": srcs or ["untagged"],
                    "b_agent": (p_agent - won) ** 2, "b_mkt": (p_mkt - won) ** 2,
                    "pnl": r.get("pnl_usd")})
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--min-n", type=int, default=20)
    a = ap.parse_args()
    rows = (_rows("journal/forecasts.jsonl", "note", "forecast")
            + _rows("journal/ledger.jsonl", "rationale", "bet"))
    agg = defaultdict(lambda: {"n": 0, "ba": 0.0, "bm": 0.0, "pnl": 0.0, "bets": 0})
    for r in rows:
        for s in r["srcs"]:
            g = agg[(s, r["kind"])]
            g["n"] += 1
            g["ba"] += r["b_agent"]
            g["bm"] += r["b_mkt"]
            if r["pnl"] is not None:
                g["pnl"] += r["pnl"]
                g["bets"] += 1
    res = []
    for (s, kind), g in sorted(agg.items()):
        n = g["n"]
        d = (g["ba"] - g["bm"]) / n
        res.append({"source": s, "kind": kind, "n": n,
                    "brier_agent": round(g["ba"] / n, 4), "brier_market": round(g["bm"] / n, 4),
                    "brier_delta": round(d, 4),
                    "pnl_usd": round(g["pnl"], 2) if g["bets"] else None,
                    "verdict": ("helps" if d < 0 else "hurts") if n >= a.min_n else "too-few"})
    if a.json:
        print(json.dumps(res))
        return
    print(f"{'source':<14}{'kind':<10}{'n':>5}{'agent':>9}{'market':>9}{'delta':>9}{'pnl':>9}  verdict")
    for r in res:
        pnl = "" if r["pnl_usd"] is None else f"{r['pnl_usd']:+.2f}"
        print(f"{r['source']:<14}{r['kind']:<10}{r['n']:>5}{r['brier_agent']:>9.4f}"
              f"{r['brier_market']:>9.4f}{r['brier_delta']:>+9.4f}{pnl:>9}  {r['verdict']}")
    print("\ndelta < 0 = agent beat the market on rows using that source. "
          "Tag rows with [src:...] in --note / rationale to be counted.")


if __name__ == "__main__":
    main()
