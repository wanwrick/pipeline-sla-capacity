"""What the capacity costs, and what missing the SLA costs.

Queueing says how many workers hold a latency target. It does not say whether
that target is worth holding. This module puts a price on both sides so the
answer to "can we run it hotter to save money" stops being a matter of nerve.

The shape of the answer is a newsvendor: too little capacity and you pay for
breaches, too much and you pay for idle compute. The optimum is where the
marginal cost of one more worker equals the marginal breach cost it prevents,
and on realistic numbers it sits a long way below the utilization most capacity
plans aim for.

Frameworks: newsvendor and cost-of-capacity trade-offs, Operations Management
(Prof. Yao Cui). Break-even and relevant-cost analysis, Managerial Accounting
(Prof. Danny Szpiro).
"""

from __future__ import annotations

from dataclasses import dataclass

from .pipeline import Pipeline
from .queueing import sojourn_time
from .simulate import DEFAULT_SEED, sla_attainment

# Indexed to 70% because that is where most capacity plans think they are safe.
UTILIZATION_LEVELS = (0.50, 0.60, 0.70, 0.80, 0.85, 0.90, 0.95, 0.98)


@dataclass(frozen=True)
class CostModel:
    """Prices, per month."""

    cost_per_server_month: float
    breach_cost: float                  # cost of one batch landing late
    batches_per_month: float
    sla_credit_threshold: float = 0.99  # attainment below this triggers a credit
    sla_credit: float = 0.0             # contractual credit if the threshold is missed

    def capacity_cost(self, servers: int) -> float:
        return servers * self.cost_per_server_month

    def breach_cost_at(self, attainment: float) -> float:
        breached = max(0.0, 1.0 - attainment) * self.batches_per_month
        penalty = self.sla_credit if attainment < self.sla_credit_threshold else 0.0
        return breached * self.breach_cost + penalty


@dataclass
class CostPoint:
    servers: int
    utilization: float
    attainment: float
    capacity_cost: float
    breach_cost: float

    @property
    def total_cost(self) -> float:
        return self.capacity_cost + self.breach_cost


def cost_curve(
    pipeline: Pipeline,
    costs: CostModel,
    stage_name: str,
    server_range: range,
    *,
    batches: int = 6_000,
    seed: int = DEFAULT_SEED,
) -> list[CostPoint]:
    """Total cost against capacity at one stage.

    Sweeping a single stage rather than the whole pipeline is deliberate: the
    bottleneck is where the money goes, and a sweep over every combination
    produces a surface nobody reads.
    """
    points: list[CostPoint] = []
    for servers in server_range:
        candidate = pipeline.with_servers(stage_name, servers)
        stage = next(s for s in candidate.stages if s.name == stage_name)
        if not stage.workload.is_stable:
            continue

        attainment = sla_attainment(candidate, batches=batches, seed=seed)
        points.append(
            CostPoint(
                servers=servers,
                utilization=stage.workload.utilization,
                attainment=attainment,
                capacity_cost=costs.capacity_cost(candidate.total_servers),
                breach_cost=costs.breach_cost_at(attainment),
            )
        )
    return points


def cheapest(points: list[CostPoint]) -> CostPoint | None:
    return min(points, key=lambda p: p.total_cost) if points else None


def marginal_value_of_a_server(points: list[CostPoint]) -> list[tuple[int, float]]:
    """Change in total cost from each additional worker.

    Negative means the worker pays for itself. The first positive entry is the
    point past which capacity is being bought for comfort rather than for the
    SLA, which is a defensible place to stop.
    """
    return [
        (later.servers, later.total_cost - earlier.total_cost)
        for earlier, later in zip(points, points[1:])
    ]


@dataclass
class UtilizationRow:
    utilization: float
    latency_minutes: float
    relative_to_70: float


def utilization_cliff(pipeline: Pipeline, stage_name: str) -> list[UtilizationRow]:
    """Latency at rising utilization, indexed to 70% busy.

    The table that ends the "just run it hotter" conversation. The delay term is
    rho/(1-rho), so the last few points of utilization cost more than all the
    earlier ones combined.
    """
    workload = next(s.workload for s in pipeline.stages if s.name == stage_name)
    latency = {rho: sojourn_time(workload.at_utilization(rho)) for rho in UTILIZATION_LEVELS}
    return [
        UtilizationRow(rho, minutes, minutes / latency[0.70])
        for rho, minutes in latency.items()
    ]
