from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from plgo_options.optimization.optimizer import OptimizerV2
from plgo_options.optimization.optimizer_v3 import OptimizerV3
from plgo_options.optimization.snapshot import load_snapshot_dict


@dataclass
class OptimizerRunParams:
    risk_aversion: float = 1.0
    brokerage_txn_cost_pct: float = 5.0
    deribit_txn_cost_pct: float = 0.1
    max_collateral: float = 4_000_000.0
    target_expiry: str | None = None
    lambda_delta: float = 001.0
    lambda_gamma: float = 1.0
    lambda_vega: float = 1.0
    unwind_discount: float = 0.2
    new_position_penalty: float = 0.04
    vega_cross_expiry_corr: float = 0.8


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
        return_val = cls(
            today=datetime.today(),
            optimizer_input={
                "eth_spot": portfolio_payload["eth_spot"],
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
        return_val = cls(
            today=today,
            optimizer_input=data["optimizer_input"],
            run_params=OptimizerRunParams(**data["run_params"]),
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
        optimizer = self.build_optimizer(self.today)

        #self.run_params.lambda_delta = -2500.
        #self.run_params.lambda_gamma = -0.5
        self.result = optimizer.run(**asdict(self.run_params))
        return self.result
        return optimizer.run()

        self.run_params.lambda_delta = 0.
        self.run_params.lambda_gamma = 1000000.
        self.run_params.lambda_vega = 100000000.
        self.run_params.max_collateral = 400_000_000.0
        self.result = optimizer.run(**asdict(self.run_params))
        return self.result
