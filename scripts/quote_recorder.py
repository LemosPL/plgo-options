#!/usr/bin/env python3
"""Run the quote recorder locally, or summarise what it has recorded.

The recorder itself is src/plgo_options/market_data/quote_recorder.py; on the server
Cloud Scheduler calls POST /api/quote-recorder/tick every minute (deploy/agents_scheduler.sh)
and files land under DB_DIR/quote_recorder.

    .venv/bin/python -u scripts/quote_recorder.py --days 14        # back-to-back files, Ctrl-C stops
    .venv/bin/python -u scripts/quote_recorder.py --once           # one 55 s file
    .venv/bin/python -u scripts/quote_recorder.py --summary        # local recordings
    .venv/bin/python -u scripts/quote_recorder.py --summary --dir ~/Downloads/quotes   # unzipped
                                                    # /api/quote-recorder/day/<day> downloads

Local files go to data/cache/quote_recorder/ (ignored by git). On a laptop, run it under
caffeinate -i so idle sleep does not stop it (closing the lid still does).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from plgo_options.market_data import quote_recorder as rec  # noqa: E402


def summary(root: Path) -> None:
    per_day = rec.days(root)
    if not per_day:
        print(f"nothing recorded under {root}")
        return
    for day, d in per_day.items():
        print(f"{day}  {d['files']:5} files  {d['MB']:8.1f} MB")
    last = max(root.glob("*/*.csv.gz"))
    df = rec.load(last)
    q = df[df.instrument != "index"]
    span = (df.recv_ms.max() - df.recv_ms.min()) / 1000
    print(f"\nlast file {last.relative_to(root)}: {len(q):,} quote rows over {span:.0f} s, "
          f"{q.instrument.nunique()} instruments, {(df.instrument == 'index').sum()} index rows")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--days", type=float, default=14.0, help="stop after this many days")
    p.add_argument("--once", action="store_true")
    p.add_argument("--summary", action="store_true")
    p.add_argument("--dir", type=Path, help="recordings root (default: as the server would choose)")
    args = p.parse_args()
    root = (args.dir or rec.data_root()).expanduser()
    if args.summary:
        summary(root)
        return
    end = time.time() + args.days * 86400
    while True:
        try:
            r = asyncio.run(rec.record(root=root))
            print(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {r['rows']:6,} rows  {r['bytes'] / 1e3:6.0f} KB  "
                  f"{r['file']}{'  ' + r['error'] if r['error'] else ''}", flush=True)
        except Exception:
            traceback.print_exc()
            time.sleep(10)  # Deribit or the network is down: try again shortly
        if args.once or time.time() >= end:
            break


if __name__ == "__main__":
    main()
