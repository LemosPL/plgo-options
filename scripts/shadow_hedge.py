#!/usr/bin/env python3
"""Shadow run of the phase-1 Deribit hedge bot on live ETH data.

Reads the live ETH book and perp hedge from the local DB, checks the delta
band, and - when breached - works the hedge on a paper connector priced off
live Deribit ETH-PERPETUAL. Nothing is sent to Deribit and nothing is booked.
Every tick is stored as an agent run ("shadow_hedge"); see
plgo_options/execution/shadow.py.

    .venv/bin/python -u scripts/shadow_hedge.py              # one tick
    .venv/bin/python -u scripts/shadow_hedge.py --every 5    # a tick every 5 min, Ctrl-C stops
    .venv/bin/python -u scripts/shadow_hedge.py --history 20 # the last 20 stored ticks
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from plgo_options.agents import store  # noqa: E402
from plgo_options.agents.store import init_agent_tables  # noqa: E402
from plgo_options.data.database import init_db  # noqa: E402
from plgo_options.execution.shadow import AGENT, run_and_store  # noqa: E402


async def main(args: argparse.Namespace) -> None:
    await init_db()
    await init_agent_tables()
    if args.history:
        for r in reversed(await store.list_runs(AGENT, args.history)):
            print(f"{r['started_at']}  {r['status']:5}  {r['text']}", flush=True)
        return
    while True:
        out = await run_and_store(max_slippage_bps=args.slippage_bps)
        print(out["text"], flush=True)
        if not args.every:
            return
        await asyncio.sleep(args.every * 60)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--every", type=float, default=0, help="minutes between ticks (0 = once)")
    p.add_argument("--slippage-bps", type=float, default=10.0, help="hedge limit vs mid")
    p.add_argument("--history", type=int, default=0, help="print the last N stored ticks")
    try:
        asyncio.run(main(p.parse_args()))
    except KeyboardInterrupt:
        pass
    # The app's imports leave a non-daemon thread that blocks interpreter exit.
    os._exit(0)
