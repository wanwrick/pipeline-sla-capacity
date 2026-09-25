"""Plan capacity against attainment, not against the mean.

The closed-form model can size a pipeline to a mean latency directly, and the
first planner written here did exactly that. It was the wrong target. A
pipeline whose mean sits comfortably inside a 15-minute promise can still miss
that promise a third of the time, because the SLA is a percentile and the mean
says nothing about the tail.

So the planner here is a hybrid. Candidates are ranked by the analytic gain,
which is free and closely tracks the real ordering, and termination is decided
by simulation, which is the only thing that can measure attainment. Ranking
analytically and stopping empirically keeps the search cheap without letting it
declare victory on the wrong statistic.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from .pipeline import Pipeline
from .simulate import DEFAULT_SEED, simulate_pipeline, sla_attainment


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
        return dict(Counter(step.stage_name for step in self.steps))

    @property
    def total_added(self) -> int:
        return len(self.steps)


def plan_for_attainment(
    pipeline: Pipeline,
    target: float = 0.99,
    *,
    max_additions: int = 24,
    batches: int = 6_000,
    replications: int = 4,
    seed: int = DEFAULT_SEED,
) -> AttainmentPlan:
    """Add workers one at a time until the simulated attainment clears the target.

    Each step adds the worker with the largest analytic latency gain, then
    re-measures attainment. The plan is a sequence rather than a lump sum, so a
    team can stop partway and still know exactly what it bought.
    """
    budget = {"batches": batches, "replications": replications, "seed": seed}
    current = pipeline
    start = sla_attainment(current, **budget)
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
        attainment = sla_attainment(current, **budget)
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
    seed: int = DEFAULT_SEED,
) -> tuple[float, float]:
    """Attainment before and after cutting one stage's service variability.

    The comparison worth putting next to a capacity request. Variability enters
    Kingman's formula linearly, so halving it halves that stage's wait at every
    utilization, and it is often an engineering change rather than an invoice.
    """
    budget = {"batches": batches, "replications": replications, "seed": seed}
    before = sla_attainment(pipeline, **budget)
    after = sla_attainment(pipeline.with_service_cv(stage_name, improved_cv), **budget)
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
    target_attainment: float

    @property
    def is_reachable(self) -> bool:
        return self.attainment_ceiling >= self.target_attainment


def attainment_ceiling(
    pipeline: Pipeline,
    *,
    target_attainment: float = 0.99,
    multiplier: int = 12,
    batches: int = 8_000,
    replications: int = 4,
    seed: int = DEFAULT_SEED,
) -> CapacityCeiling:
    """Attainment with queueing driven out by overwhelming capacity.

    The percentiles of the flooded run are the SLAs this design could actually
    hold: a number the pipeline can keep is worth more than one that sounds
    good in a contract and breaches every week.
    """
    flooded = pipeline.with_server_multiplier(multiplier)
    result = simulate_pipeline(flooded, batches=batches, seed=seed, replications=replications)
    return CapacityCeiling(
        attainment_ceiling=result.attainment(pipeline.sla_minutes),
        floor_mean=result.mean_sojourn,
        floor_p95=result.percentile(0.95),
        floor_p99=result.percentile(0.99),
        target_attainment=target_attainment,
    )
