"""Load a pipeline scenario from YAML."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .economics import CostModel
from .pipeline import Pipeline, Stage
from .queueing import Workload

SCENARIO_DIR = Path(__file__).resolve().parents[1] / "scenarios"
DEFAULT_SCENARIO = SCENARIO_DIR / "customer_360.yaml"


def load_raw(path: Path = DEFAULT_SCENARIO) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def load_pipeline(path: Path = DEFAULT_SCENARIO) -> Pipeline:
    raw = load_raw(path)
    stages = [
        Stage(
            name=entry["name"],
            note=" ".join(entry.get("note", "").split()),
            workload=Workload(
                arrival_rate=float(entry["arrival_rate"]),
                service_time=float(entry["service_time"]),
                servers=int(entry["servers"]),
                arrival_cv=float(entry.get("arrival_cv", 1.0)),
                service_cv=float(entry.get("service_cv", 1.0)),
            ),
        )
        for entry in raw["stages"]
    ]
    return Pipeline(
        name=raw["name"], sla_minutes=float(raw["sla_minutes"]), stages=stages
    )


def load_costs(path: Path = DEFAULT_SCENARIO) -> CostModel:
    raw = load_raw(path)["costs"]
    return CostModel(
        cost_per_server_month=float(raw["cost_per_server_month"]),
        breach_cost=float(raw["breach_cost"]),
        batches_per_month=float(raw["batches_per_month"]),
        sla_credit_threshold=float(raw.get("sla_credit_threshold", 0.99)),
        sla_credit=float(raw.get("sla_credit", 0.0)),
    )


def load_simulation_config(path: Path = DEFAULT_SCENARIO) -> dict[str, int]:
    raw = load_raw(path).get("simulation", {})
    return {
        "batches": int(raw.get("batches", 20_000)),
        "warmup": int(raw.get("warmup", 2_000)),
        "seed": int(raw.get("seed", 20260916)),
    }


def load_peak_multipliers(path: Path = DEFAULT_SCENARIO) -> list[float]:
    return [float(m) for m in load_raw(path).get("peak_multipliers", [1.0])]
