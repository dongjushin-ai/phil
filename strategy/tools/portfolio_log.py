#!/usr/bin/env python3
"""Append one row per cycle run to journal/portfolio.md (operator-facing run log, Korean).

Usage (after the cycle's log line, before the cycle commit):
  python3 strategy/tools/portfolio_log.py --tick FULL --placed 0 --settled 0 \
      [--forecasts-settled 12] [--note "한 줄 메모"]

Reads cash/open positions from `core/ledger.py status` and the headline from
`core/score.py`, so the row always matches the protected engine's numbers.
Run time is recorded in KST (operator's zone) with UTC alongside.
"""
import argparse
import datetime as dt
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PATH = os.path.join(ROOT, "journal", "portfolio.md")
KST = dt.timezone(dt.timedelta(hours=9))

HEADER = """# Phil 모의투자 포트폴리오 실행 기록

매 사이클(1시간마다, 클라우드 실행)이 끝날 때 한 줄씩 자동으로 추가됩니다.
금액은 모의(paper) 계좌 기준이며 실제 돈이 아닙니다. 시작 자금 $1,000.

- 현금: `core/ledger.py status`의 cash (미정산 포지션 매입액 제외)
- 누적 성적: `core/score.py` 첫 줄(정산 건수, 누적 손익, 브라이어 점수 차이: 양수면 시장보다 예측이 부정확)

| 실행 시각 (KST) | UTC | 틱 | 신규 베팅 | 정산 베팅 | 정산 예측 | 현금 | 보유 포지션 | 누적 정산 / 손익 | 브라이어 vs 시장 | 메모 |
|---|---|---|---|---|---|---|---|---|---|---|
"""


def run(args):
    return subprocess.run([sys.executable] + args, cwd=ROOT, capture_output=True, text=True).stdout


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tick", required=True)
    ap.add_argument("--placed", type=int, required=True)
    ap.add_argument("--settled", type=int, required=True)
    ap.add_argument("--forecasts-settled", default="")
    ap.add_argument("--note", default="")
    ap.add_argument("--when", help="UTC ISO time (default now)")
    a = ap.parse_args()

    status = json.loads(run(["core/ledger.py", "status"]))
    score_lines = run(["core/score.py"]).splitlines()
    head = next((l for l in score_lines if l.startswith("settled=")), "")
    brier = next((l for l in score_lines if l.startswith("brier:")), "")
    kv = dict(p.split("=", 1) for p in head.split() if "=" in p)
    delta = next((p.split("=", 1)[1] for p in brier.split() if p.startswith("delta=")), "?")

    t = dt.datetime.fromisoformat(a.when.replace("Z", "+00:00")) if a.when else dt.datetime.now(dt.timezone.utc)
    row = "| {} | {} | {} | {} | {} | {} | ${:,.2f} | {} | {}건 / {} | {} | {} |\n".format(
        t.astimezone(KST).strftime("%Y-%m-%d %H:%M"),
        t.astimezone(dt.timezone.utc).strftime("%m-%d %H:%MZ"),
        a.tick, a.placed, a.settled, a.forecasts_settled or "-",
        status["cash"], status["open_positions"],
        kv.get("settled", "?"), kv.get("pnl", "?"), delta,
        a.note.replace("|", "/") or "-",
    )
    if not os.path.exists(PATH):
        os.makedirs(os.path.dirname(PATH), exist_ok=True)
        open(PATH, "w").write(HEADER)
    with open(PATH, "a") as f:
        f.write(row)
    print(row, end="")


if __name__ == "__main__":
    main()
