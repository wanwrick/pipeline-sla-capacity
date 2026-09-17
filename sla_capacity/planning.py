"""Plan capacity against attainment, not against the mean.

`cheapest_path_to_sla` in pipeline.py sizes to a mean latency, which is what the
closed-form model can answer directly. It is also the wrong target. A pipeline
whose mean sits comfortably inside a 15-minute promise can still miss that
promise a third of the time, because the SLA is a percentile and the mean says
nothing about the tail.

So the planner here is a hybrid. Candidates are ranked by the analytic gain,
which is free and closely tracks the real ordering, and termination is decided
by simulation, which is the only thing that can measure attainment. Ranking
analytically and stopping empirically keeps the search cheap without letting it
declare victory on the wrong statistic.
"""

from __future__ import annotations

from dataclasses import dataclass

from .pipeline import Pipeline
from .simulate import simulate_pipeline


@dataclass
class AttainmentStep:
    stage_name: str
    servers_after: int
    attainment_after: float
    mean_after: float


@dataclass
class AttainmentPlan:
    steps: list[AttainmentStep]
    final: Pipeline
    start_attainment: float
    final_attainment: float
    reached_target: bool

    @property
    def servers_added(self) -> dict[str, int]:
        totals: dict[str, int] = {}
        for step in self.steps:
            totals[step.stage_name] = totals.get(step.stage_name, 0) + 1
        return totals

    @property
    def total_added(self) -> int:
        return len(self.steps)


def _attainment(
    pipeline: Pipeline, batches: int, replications: int, seed: int
) -> float:
    result = simulate_pipeline(
        pipeline,
        batches=batches,
        warmup=batches // 10,
        seed=seed,
        replications=replications,
    )
    return result.attainment(pipeline.sla_minutes)


def plan_for_attainment(
    pipeline: Pipeline,
    target: float = 0.99,
    *,
    max_additions: int = 24,
    batches: int = 6_000,
    replications: int = 4,
    seed: int = 20260916,
) -> AttainmentPlan:
    """Add workers one at a time until the simulated attainment clears the target.

    Each step adds the worker with the largest analytic latency gain, then
    re-measures attainment. The plan is a sequence rather than a lump sum, so a
    team can stop partway and still know exactly what it bought.
    """
    current = pipeline
    start = _attainment(current, batches, replications, seed)
    attainment = start
    steps: list[AttainmentStep] = []

    for _ in range(max_additions):
        if attainment >= target:
            break

        analytic_now = current.evaluate().total_latency
        best: tuple[float, str, Pipeline] | None = None
        for stage in current.stages:
            candidate = current.with_servers(stage.name, stage.workload.servers + 1)
            gain = analytic_now - candidate.evaluate().total_latency
            if gain > 0 and (best is None or gain > best[0]):
                best = (gain, stage.name, candidate)

        if best is None:
            break  # No single worker helps; the constraint is variability.

        _, stage_name, current = best
        attainment = _attainment(current, batches, replications, seed)
        servers = next(s for s in current.stages if s.name == stage_name).workload.servers
        steps.append(
            AttainmentStep(
                stage_name=stage_name,
                servers_after=servers,
                attainment_after=attainment,
                mean_after=current.evaluate().total_latency,
            )
        )

    return AttainmentPlan(
        steps=steps,
        final=current,
        start_attainment=start,
        final_attainment=attainment,
        reached_target=attainment >= target,
    )


def variability_alternative(
    pipeline: Pipeline,
    stage_name: str,
    improved_cv: float,
    *,
    batches: int = 6_000,
    replications: int = 4,
    seed: int = 20260916,
) -> tuple[float, float]:
    """Attainment before and after cutting one stage's service variability.

    The comparison worth putting next to a capacity request. Variability enters
    Kingman's formula linearly, so halving it halves that stage's wait at every
    utilization, and it is often an engineering change rather than an invoice.
    """
    before = _attainment(pipeline, batches, replications, seed)
    improved = pipeline.with_service_cv(stage_name, improved_cv)
    after = _attainment(improved, batches, replications, seed)
    return before, after


# --- The ceiling capacity cannot pass -------------------------------------------


@dataclass
class CapacityCeiling:
    """What the design can do with queueing removed entirely.

    Drive servers high enough and waiting time goes to zero. What remains is the
    service time distribution itself, and if that distribution already breaches
    the SLA then no capacity plan will ever hold it. This is the first thing to
    check and almost nobody checks it, which is how teams end up buying compute
    for a target the architecture cannot reach.
    """

    attainment_ceiling: float
    floor_mean: float
    floor_p95: float
    floor_p99: float
    sla_minutes: float

    @property
    def is_reachable(self) -> bool:
        return self.attainment_ceiling >= 0.99

    @property
    def service_time_floor_exceeds_sla(self) -> bool:
        return self.floor_p99 > self.sla_minutes


def attainment_ceiling(
    pipeline: Pipeline,
    *,
    multiplier: int = 12,
    batches: int = 8_000,
    replications: int = 4,
    seed: int = 20260916,
) -> CapacityCeiling:
    """Attainment with queueing driven out by overwhelming capacity."""
    flooded = pipeline
    for stage in pipeline.stages:
        flooded = flooded.with_servers(stage.name, stage.workload.servers * multiplier)

    result = simulate_pipeline(
        flooded,
        batches=batches,
        warmup=batches // 10,
        seed=seed,
        replications=replications,
    )
    return CapacityCeiling(
        attainment_ceiling=result.attainment(pipeline.sla_minutes),
        floor_mean=result.mean_sojourn,
        floor_p95=result.percentile(0.95),
        floor_p99=result.percentile(0.99),
        sla_minutes=pipeline.sla_minutes,
    )


def achievable_sla(
    pipeline: Pipeline,
    target_attainment: float = 0.99,
    *,
    multiplier: int = 12,
    batches: int = 8_000,
    replications: int = 4,
    seed: int = 20260916,
) -> float:
    """The SLA this design could actually hold at the target attainment.

    Useful when the answer is that the promise is wrong rather than the
    capacity. A number the pipeline can hold is worth more than a number that
    sounds good in a contract and breaches every week.
    """
    flooded = pipeline
    for stage in pipeline.stages:
        flooded = flooded.with_servers(stage.name, stage.workload.servers * multiplier)

    result = simulate_pipeline(
        flooded,
        batches=batches,
        warmup=batches // 10,
        seed=seed,
        replications=replications,
    )
    return result.percentile(target_attainment)


def service_time_reduction_needed(
    pipeline: Pipeline,
    stage_name: str,
    target_attainment: float = 0.99,
    *,
    batches: int = 6_000,
    replications: int = 3,
    seed: int = 20260916,
    steps: int = 12,
) -> float | None:
    """The share a stage's service time must fall by to reach the target.

    Returns None when cutting that stage to nothing still misses, which means
    the constraint is spread across the pipeline rather than sitting in one
    place. Capacity is generous here so the answer isolates service time.
    """
    from dataclasses import replace as _replace

    generous = pipeline
    for stage in pipeline.stages:
        generous = generous.with_servers(stage.name, stage.workload.servers * 4)

    target_stage = next(s for s in generous.stages if s.name == stage_name)
    original = target_stage.workload.service_time

    for step in range(steps + 1):
        reduction = step / steps
        candidate = _replace(
            generous,
            stages=[
                _replace(
                    s,
                    workload=_replace(
                        s.workload, service_time=max(0.01, original * (1.0 - reduction))
                    ),
                )
                if s.name == stage_name
                else s
                for s in generous.stages
            ],
        )
        if _attainment(candidate, batches, replications, seed) >= target_attainment:
            return reduction
    return None
