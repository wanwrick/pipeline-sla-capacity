"""A pipeline as a series of queues, and the bottleneck that governs it.

End-to-end freshness is the sum of what each stage contributes, but capacity
should almost never be added evenly. One stage sets the pace. Adding workers
anywhere else buys nothing and shows up on the invoice anyway.

Frameworks: process analysis and bottleneck identification, Operations
Management (Prof. Yao Cui, Cornell).
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from .queueing import (
    Workload,
    kingman_wait_time,
    queue_length,
    servers_for_target_latency,
    sojourn_time,
    utilization_for_target_latency,
    vut_factors,
)


@dataclass(frozen=True)
class Stage:
    name: str
    workload: Workload
    note: str = ""


@dataclass
class StageResult:
    stage: Stage
    wait_minutes: float
    sojourn_minutes: float
    utilization: float
    queue_depth: float
    variability: float
    utilization_term: float

    @property
    def name(self) -> str:
        return self.stage.name

    @property
    def is_stable(self) -> bool:
        return self.stage.workload.is_stable


@dataclass
class PipelineResult:
    stages: list[StageResult]
    sla_minutes: float

    @property
    def total_latency(self) -> float:
        return sum(s.sojourn_minutes for s in self.stages)

    @property
    def meets_sla(self) -> bool:
        return self.total_latency <= self.sla_minutes

    @property
    def headroom_minutes(self) -> float:
        return self.sla_minutes - self.total_latency

    @property
    def bottleneck(self) -> StageResult:
        """The stage contributing the most delay, not the busiest one.

        These are usually the same stage and occasionally are not. When a stage
        runs hot but finishes fast it is not the constraint, and a capacity plan
        built off a utilization chart alone will fund the wrong thing.
        """
        return max(self.stages, key=lambda s: s.sojourn_minutes)

    @property
    def busiest(self) -> StageResult:
        return max(self.stages, key=lambda s: s.utilization)

    def share_of_latency(self, stage: StageResult) -> float:
        total = self.total_latency
        return stage.sojourn_minutes / total if total else 0.0


@dataclass(frozen=True)
class Pipeline:
    name: str
    sla_minutes: float
    stages: list[Stage]

    def evaluate(self) -> PipelineResult:
        results = []
        for stage in self.stages:
            variability, utilization_term, _ = vut_factors(stage.workload)
            results.append(
                StageResult(
                    stage=stage,
                    wait_minutes=kingman_wait_time(stage.workload),
                    sojourn_minutes=sojourn_time(stage.workload),
                    utilization=stage.workload.utilization,
                    queue_depth=queue_length(stage.workload),
                    variability=variability,
                    utilization_term=utilization_term,
                )
            )
        return PipelineResult(stages=results, sla_minutes=self.sla_minutes)

    def with_servers(self, stage_name: str, servers: int) -> "Pipeline":
        stages = [
            replace(s, workload=replace(s.workload, servers=servers))
            if s.name == stage_name
            else s
            for s in self.stages
        ]
        return replace(self, stages=stages)

    def with_service_cv(self, stage_name: str, cv: float) -> "Pipeline":
        stages = [
            replace(s, workload=replace(s.workload, service_cv=cv))
            if s.name == stage_name
            else s
            for s in self.stages
        ]
        return replace(self, stages=stages)

    def with_arrival_multiplier(self, multiplier: float) -> "Pipeline":
        """Scale demand across every stage, for a peak-load view."""
        stages = [
            replace(s, workload=replace(s.workload, arrival_rate=s.workload.arrival_rate * multiplier))
            for s in self.stages
        ]
        return replace(self, stages=stages)


@dataclass
class Addition:
    stage_name: str
    servers_added: int
    minutes_saved: float
    latency_after: float


def cheapest_path_to_sla(pipeline: Pipeline, *, max_additions: int = 60) -> list[Addition]:
    """Add one worker at a time, always to the stage that gains the most.

    A greedy walk rather than an optimizer, deliberately. The output is a
    sequence a team can actually execute and stop partway through, and each
    step states what the next worker buys. An optimizer returns a target that
    has to be funded all at once.
    """
    plan: list[Addition] = []
    current = pipeline
    latency = current.evaluate().total_latency

    for _ in range(max_additions):
        result = current.evaluate()
        if result.meets_sla:
            break

        best: tuple[float, Stage, Pipeline] | None = None
        for stage in current.stages:
            candidate = current.with_servers(stage.name, stage.workload.servers + 1)
            gain = latency - candidate.evaluate().total_latency
            if gain > 0 and (best is None or gain > best[0]):
                best = (gain, stage, candidate)

        if best is None:
            break  # No single addition helps; the constraint is elsewhere.

        gain, stage, current = best
        latency = current.evaluate().total_latency
        plan.append(
            Addition(
                stage_name=stage.name,
                servers_added=1,
                minutes_saved=gain,
                latency_after=latency,
            )
        )

    return plan


def consolidate(plan: list[Addition]) -> dict[str, int]:
    """Collapse the step-by-step plan into workers per stage."""
    totals: dict[str, int] = {}
    for addition in plan:
        totals[addition.stage_name] = totals.get(addition.stage_name, 0) + addition.servers_added
    return totals


def stage_target_utilizations(pipeline: Pipeline) -> dict[str, float | None]:
    """Per stage, the utilization that keeps the pipeline inside its SLA.

    The SLA budget is split across stages in proportion to each stage's current
    contribution, which is the split that requires no stage to improve more
    than it already carries.
    """
    result = pipeline.evaluate()
    total = result.total_latency
    targets: dict[str, float | None] = {}
    for stage_result in result.stages:
        share = stage_result.sojourn_minutes / total if total else 0.0
        budget = pipeline.sla_minutes * share
        targets[stage_result.name] = utilization_for_target_latency(
            stage_result.stage.workload, budget
        )
    return targets


def servers_to_hold_peak(pipeline: Pipeline, peak_multiplier: float) -> dict[str, int | None]:
    """Servers each stage needs to hold its current latency at peak demand."""
    baseline = {s.name: sojourn_time(s.workload) for s in pipeline.stages}
    peaked = pipeline.with_arrival_multiplier(peak_multiplier)
    return {
        s.name: servers_for_target_latency(s.workload, baseline[s.name])
        for s in peaked.stages
    }
