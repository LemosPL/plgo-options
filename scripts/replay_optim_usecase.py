from __future__ import annotations

import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

import numpy as np

from plgo_options.optimization.optimizer import OptimizerV2

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from plgo_options.optimization.optim_usecase import OptimizerUseCase  # noqa: E402


def _json_default(obj):
    if hasattr(obj, "item"):
        return obj.item()
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")

def build_optimizer(self) -> OptimizerV2:
    return OptimizerV2.from_snapshot_dict(self.optimizer_input)

def run(self) -> dict[str, Any]:
    optimizer = self.build_optimizer()
    self.result = optimizer.run(**asdict(self.run_params))
    return self.result

def main() -> None:
    if len(sys.argv) < 2:
        path = Path("../data/optimization_snapshots/usecases/20260323_131339_ALL.json")
    else:
        path = Path(sys.argv[1])
    usecase = OptimizerUseCase.load(path)

    result = usecase.run()

    #out_path = path.with_name(path.stem + "_replayed.json")
    #out_path.write_text(json.dumps(result, indent=2, default=_json_default))
    #print(f"Saved replay result to: {out_path}")
    print(f"status: {result.get('status')}")
    trades = result.get('trades', [])
    print(len(trades))
    print(f"trades: {len(trades or [])}")
    print(np.array(trades, dtype=object))

if __name__ == "__main__":
    main()