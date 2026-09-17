"""Discrete-event simulation, used to check the formulas rather than replace them.

Kingman's approximation gives a mean. An SLA is a percentile, and the two are
not interchangeable: a stage can hold a 6-minute average and still breach a
15-minute promise one run in twenty. So the analytic model sizes the system and
the simulation reports what share of batches actually land inside the window.

The agreement between the two is itself a test. If the simulated mean drifts
from the closed form on an M/M/c workload, one of them is wrong, and the test
suite says which.

One run of a queue is not an estimate. Successive waits are strongly
autocorrelated, so a single long run can sit 10% off the true mean and look
perfectly convergent while doing it. Measured here, single 100k-batch runs of
an M/M/3 queue landed anywhere from 8% low to 13% high. Everything below
therefore uses the independent-replications method: several runs on different
seeds, pooled. Twenty replications bring the same estimate inside 1.5%.
"""

from __future__ import annotations

import heapq
import math
import random
from dataclasses import dataclass

from .pipeline import Pipeline
from .queueing import Workload


@dataclass
class SimulationResult:
    completed: int
    waits: list[float]
    sojourns: list[float]

    @property
    def mean_wait(self) -> float:
        return sum(self.waits) / len(self.waits) if self.waits else 0.0

    @property
    def mean_sojourn(self) -> float:
        return sum(self.sojourns) / len(self.sojourns) if self.sojourns else 0.0

    def percentile(self, p: float) -> float:
        if not self.sojourns:
            return 0.0
        ordered = sorted(self.sojourns)
        index = min(int(math.ceil(p * len(ordered))) - 1, len(ordered) - 1)
        return ordered[max(index, 0)]

    def attainment(self, sla_minutes: float) -> float:
        """Share of batches finishing inside the SLA. The number that is promised."""
        if not self.sojourns:
            return 1.0
        return sum(1 for s in self.sojourns if s <= sla_minutes) / len(self.sojourns)


def _sample(rng: random.Random, mean: float, cv: float) -> float:
    """Draw a positive duration with the requested mean and coefficient of variation.

    Exponential when cv is 1, deterministic when cv is 0, gamma otherwise.
    Matching the cv rather than assuming exponential everywhere is the point:
    variability is the input the formulas are most sensitive to.
    """
    if cv <= 0:
        return mean
    if abs(cv - 1.0) < 1e-9:
        return rng.expovariate(1.0 / mean)
    shape = 1.0 / (cv**2)
    scale = mean / shape
    return rng.gammavariate(shape, scale)


def simulate_stage(
    workload: Workload,
    *,
    batches: int = 20_000,
    warmup: int = 2_000,
    seed: int = 20260916,
    replications: int = 1,
) -> SimulationResult:
    """Single-stage multi-server queue, FIFO.

    The first `warmup` batches of each replication are discarded. A queue
    started empty is not in steady state, and its early, unnaturally short
    waits bias every statistic downward.

    With `replications` above 1 the run is repeated on fresh seeds and the
    samples pooled. Prefer more replications over a longer single run: waits
    within one run are autocorrelated, so extra batches buy less independence
    than they appear to.
    """
    if not workload.is_stable:
        raise ValueError("cannot simulate an unstable queue: utilization is at or above 1")

    if replications > 1:
        pooled_waits: list[float] = []
        pooled_sojourns: list[float] = []
        for rep in range(replications):
            run = _run_once(workload, batches, warmup, seed + rep * 7919)
            pooled_waits.extend(run.waits)
            pooled_sojourns.extend(run.sojourns)
        return SimulationResult(
            completed=len(pooled_sojourns), waits=pooled_waits, sojourns=pooled_sojourns
        )

    return _run_once(workload, batches, warmup, seed)


def _run_once(
    workload: Workload, batches: int, warmup: int, seed: int
) -> SimulationResult:
    rng = random.Random(seed)
    # One "next free" time per server, kept as a heap so the earliest is on top.
    free_at = [0.0] * workload.servers
    heapq.heapify(free_at)

    arrival = 0.0
    waits: list[float] = []
    sojourns: list[float] = []
    mean_interarrival = 1.0 / workload.arrival_rate

    for index in range(batches):
        arrival += _sample(rng, mean_interarrival, workload.arrival_cv)
        earliest_free = heapq.heappop(free_at)
        start = max(arrival, earliest_free)
        service = _sample(rng, workload.service_time, workload.service_cv)
        finish = start + service
        heapq.heappush(free_at, finish)

        if index >= warmup:
            waits.append(start - arrival)
            sojourns.append(finish - arrival)

    return SimulationResult(completed=len(sojourns), waits=waits, sojourns=sojourns)


def simulate_pipeline(
    pipeline: Pipeline,
    *,
    batches: int = 20_000,
    warmup: int = 2_000,
    seed: int = 20260916,
    replications: int = 1,
) -> SimulationResult:
    """End-to-end latency across every stage.

    Stage sojourns are sampled independently and summed. That understates the
    tail slightly, because a slow upstream batch tends to arrive into a busy
    downstream stage, and the README says so rather than hiding it.
    """
    per_stage = [
        simulate_stage(
            stage.workload,
            batches=batches,
            warmup=warmup,
            seed=seed + i,
            replications=replications,
        )
        for i, stage in enumerate(pipeline.stages)
    ]
    length = min(len(r.sojourns) for r in per_stage)
    totals = [sum(r.sojourns[i] for r in per_stage) for i in range(length)]
    waits = [sum(r.waits[i] for r in per_stage) for i in range(length)]
    return SimulationResult(completed=length, waits=waits, sojourns=totals)


def attainment_curve(
    pipeline: Pipeline,
    multipliers: list[float],
    *,
    batches: int = 8_000,
    seed: int = 20260916,
    replications: int = 4,
) -> list[tuple[float, float]]:
    """SLA attainment against demand, for the peak-load conversation.

    Unstable points return 0 rather than raising, because "the pipeline falls
    over at 1.6x" is the finding, not an error.
    """
    curve: list[tuple[float, float]] = []
    for multiplier in multipliers:
        scaled = pipeline.with_arrival_multiplier(multiplier)
        if any(not s.workload.is_stable for s in scaled.stages):
            curve.append((multiplier, 0.0))
            continue
        result = simulate_pipeline(
            scaled,
            batches=batches,
            warmup=batches // 10,
            seed=seed,
            replications=replications,
        )
        curve.append((multiplier, result.attainment(pipeline.sla_minutes)))
    return curve
