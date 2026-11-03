#!/usr/bin/env python3
"""Run the smile recorder locally, or summarise what it has recorded.

The recorder itself is src/plgo_options/market_data/smile_recorder.py; on the
server Cloud Scheduler calls POST /api/smile-recorder/tick every 5 minutes
(deploy/agents_scheduler.sh) and ticks land under DB_DIR/smile_recorder.

    .venv/bin/python -u scripts/smile_recorder.py                  # every 5 min, Ctrl-C stops
    .venv/bin/python -u scripts/smile_recorder.py --once           # one tick
    .venv/bin/python -u scripts/smile_recorder.py --summary        # local recordings
    .venv/bin/python -u scripts/smile_recorder.py --summary --dir ~/Downloads/smile   # unzipped
                                                    # /api/smile-recorder/day/<day> downloads

Local ticks go to data/cache/smile_recorder/ (ignored by git).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from plgo_options.market_data import smile_recorder as rec  # noqa: E402


def summary(root: Path) -> None:
    strikes, _ = rec.load(root)
    rows = rec.summarize(strikes)
    if not rows:
        print(f"nothing recorded under {root}")
        return
    print(f"{strikes.ts.nunique()} ticks, {strikes.ts.min()} .. {strikes.ts.max()}\n")
    print(f"{'expiry':9}{'rows':>7}{'buy>0 %':>9}{'sell>0 %':>9}{'med buy vp':>12}{'med sell vp':>12}"
          f"{'med size':>10}")
    fmt = lambda x: f"{x:.2f}" if x is not None else "-"  # noqa: E731
    for r in rows:
        print(f"{r['expiry']:9}{r['rows']:>7}{r['buy_pct']:>9.1f}{r['sell_pct']:>9.1f}"
              f"{fmt(r['med_buy_vp']):>12}{fmt(r['med_sell_vp']):>12}{fmt(r['med_size']):>10}")
    print("\nvp = vol points; edges are net of the taker fee against the leave-one-out SVI fit; "
          "size = touch size (ETH) on the side taken when the edge is positive.")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--every", type=float, default=5, help="minutes between ticks, aligned to the clock")
    p.add_argument("--expiries", type=int, default=3, help="number of quarterly expiries to record")
    p.add_argument("--depth", type=int, default=10, help="order book levels to read")
    p.add_argument("--once", action="store_true", help="one tick, then exit")
    p.add_argument("--summary", action="store_true", help="summarise what has been recorded")
    p.add_argument("--dir", type=Path, default=ROOT / "data" / "cache" / "smile_recorder",
                   help="where ticks are written / read")
    args = p.parse_args()
    if args.summary:
        return summary(args.dir.expanduser())
    while True:
        try:
            out = asyncio.run(rec.record_tick(args.dir, args.expiries, args.depth))
            for f in out["fits"]:
                print(rec.tick_line(f), flush=True)
            for e, err in out["errors"].items():
                print(f"{out['asof']} {e}: {err}", flush=True)
        except Exception:  # network or API hiccup: log it and wait for the next tick
            traceback.print_exc()
        if args.once:
            return
        step = args.every * 60
        time.sleep(step - time.time() % step)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
    sys.stdout.flush()
    os._exit(0)  # app imports can leave threads that hang interpreter exit
