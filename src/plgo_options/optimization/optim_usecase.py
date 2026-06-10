from __future__ import annotations

import inspect
import json
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
import pandas as pd
import numpy as np

from plgo_options.optimization.optimizer import OptimizerV2
from plgo_options.optimization.optimizer_v3 import OptimizerV3
from plgo_options.optimization.snapshot import load_snapshot_dict


@dataclass
class OptimizerRunParams:
    asset: str = "ETH"
    lam_factor: float = 1.0
    target_expiry: str | None = "28AUG26"
    unwind_discount: float = 0.2
    new_position_penalty: float = 0.04
    roll_dte_threshold: int | None = None
    is_replay: bool = False
    counterparties: list[str] | None = None


@dataclass
class OptimizerUseCase:
    today: datetime
    optimizer_input: dict[str, Any]
    run_params: OptimizerRunParams
    result: dict[str, Any] | None = None

    @classmethod
    def from_portfolio_payload(
        cls,
        portfolio_payload: dict[str, Any],
        run_params: OptimizerRunParams,
    ) -> "OptimizerUseCase":
        spot = portfolio_payload.get("spot", portfolio_payload.get("eth_spot"))

        return_val = cls(
            today=datetime.today(),
            optimizer_input={
                "asset": portfolio_payload.get("asset", run_params.asset).upper(),
                "spot": spot,
                "eth_spot": spot,  # backward-compatible alias for current optimizer internals
                "spot_ladder": portfolio_payload["spot_ladder"],
                "matrix_horizons": portfolio_payload["matrix_horizons"],
                "chart_horizons": portfolio_payload["chart_horizons"],
                "vol_surface": portfolio_payload["vol_surface"],
                "positions": portfolio_payload["positions"],
                "totals": portfolio_payload["totals"],
                "snapshot_path": portfolio_payload.get("snapshot_path", ""),
            },
            run_params=run_params,
        )
        #print(return_val)
        return return_val

    @classmethod
    def load(cls, path: str | Path) -> "OptimizerUseCase":
        path = Path(path)
        with path.open() as f:
            data = json.load(f)

        today_str = path.name.split('_')[0]
        today = datetime.strptime(today_str, "%Y%m%d")
        valid_run_param_names = {field.name for field in fields(OptimizerRunParams)}
        run_params_data = {
            key: value
            for key, value in data["run_params"].items()
            if key in valid_run_param_names
        }

        return_val = cls(
            today=today,
            optimizer_input=data["optimizer_input"],
            run_params=OptimizerRunParams(**run_params_data),
            result=data.get("result"),
        )
        # print(return_val)
        return return_val

    def save(self, path: str | Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w") as f:
            json.dump(
                {
                    "optimizer_input": self.optimizer_input,
                    "run_params": asdict(self.run_params),
                    "result": self.result,
                },
                f,
                indent=2,
            )
        return path

    def save_auto(self, directory: str | Path) -> Path:
        directory = Path(directory)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        expiry = self.run_params.target_expiry or "ALL"
        filename = f"{ts}_{expiry}.json"
        return self.save(directory / filename)

    def build_optimizer(self, today=None) -> OptimizerV3:#OptimizerV2:
        if today is None:
            today = self.today
        return OptimizerV3.from_snapshot_dict(self.optimizer_input, today)
        return OptimizerV2.from_snapshot_dict(self.optimizer_input)

    def run(self) -> dict[str, Any]:
        print('run()')
        optimizer = self.build_optimizer(self.today)

        '''self.run_params.target_expiry = "31JUL26"
        self.run_params.lam_factor = 0.3
        self.run_params.roll_dte_threshold = 7'''
        self.result = optimizer.run(**asdict(self.run_params))
        return self.result

    def run_test(self):
        print('run_test()')
        optimizer = self.build_optimizer(self.today)
        result = optimizer.run(target_expiry="28AUG26", is_replay=True, roll_dte_threshold=12, lam_factor=0.1)#, counterparties=["Flowdesk"])

        print(f"roll_unwind_trades: {len(result.get('roll_unwind_trades', []))}")
        print(f"replacement_trades: {len(result.get('replacement_trades', []))}")
        return result
