"""A pipeline as a series of queues, and the bottleneck that governs it.

End-to-end freshness is the sum of what each stage contributes, but capacity
should almost never be added evenly. One stage sets the pace. Adding workers
anywhere else buys nothing and shows up on the invoice anyway.

Frameworks: process analysis and bottleneck identification, Operations
Management (Prof. Yao Cui, Cornell).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace

from .queueing import (
    Workload,
    kingman_wait_time,
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

    @property
    def total_servers(self) -> int:
        return sum(s.workload.servers for s in self.stages)

    def evaluate(self) -> PipelineResult:
        results = []
        for stage in self.stages:
            workload = stage.workload
            variability, utilization_term, _ = vut_factors(workload)
            wait = kingman_wait_time(workload)
            results.append(
                StageResult(
                    stage=stage,
                    wait_minutes=wait,
                    sojourn_minutes=wait + workload.service_time,
                    utilization=workload.utilization,
                    queue_depth=workload.arrival_rate * wait,  # Little's Law
                    variability=variability,
                    utilization_term=utilization_term,
                )
            )
        return PipelineResult(stages=results, sla_minutes=self.sla_minutes)

    def with_workload(self, stage_name: str, **changes) -> "Pipeline":
        """One stage's workload with the given fields replaced. The rest is untouched."""
        stages = [
            replace(s, workload=replace(s.workload, **changes)) if s.name == stage_name else s
            for s in self.stages
        ]
        return replace(self, stages=stages)

    def with_servers(self, stage_name: str, servers: int) -> "Pipeline":
        return self.with_workload(stage_name, servers=servers)

    def with_service_cv(self, stage_name: str, cv: float) -> "Pipeline":
        return self.with_workload(stage_name, service_cv=cv)

    def _with_each_workload(self, change: Callable[[Workload], Workload]) -> "Pipeline":
        return replace(self, stages=[replace(s, workload=change(s.workload)) for s in self.stages])

    def with_server_multiplier(self, multiplier: int) -> "Pipeline":
        """Scale capacity at every stage, for the queueing-removed view."""
        return self._with_each_workload(lambda w: replace(w, servers=w.servers * multiplier))

    def with_arrival_multiplier(self, multiplier: float) -> "Pipeline":
        """Scale demand across every stage, for a peak-load view."""
        return self._with_each_workload(
            lambda w: replace(w, arrival_rate=w.arrival_rate * multiplier)
        )


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
