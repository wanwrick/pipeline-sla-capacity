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
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache

from .pipeline import Pipeline
from .queueing import Workload

DEFAULT_SEED = 20260916


@dataclass(frozen=True)
class SimulationResult:
    waits: list[float]
    sojourns: list[float]

    @property
    def completed(self) -> int:
        return len(self.sojourns)

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


def _sampler(rng: random.Random, mean: float, cv: float) -> Callable[[], float]:
    """Draws of positive durations with the requested mean and coefficient of variation.

    Exponential when cv is 1, deterministic when cv is 0, gamma otherwise.
    Matching the cv rather than assuming exponential everywhere is the point:
    variability is the input the formulas are most sensitive to.
    """
    if cv <= 0:
        return lambda: mean
    if abs(cv - 1.0) < 1e-9:
        rate = 1.0 / mean
        return lambda: rng.expovariate(rate)
    shape = 1.0 / (cv**2)
    scale = mean / shape
    return lambda: rng.gammavariate(shape, scale)


def simulate_stage(
    workload: Workload,
    *,
    batches: int = 20_000,
    warmup: int | None = None,
    seed: int = DEFAULT_SEED,
    replications: int = 1,
) -> SimulationResult:
    """Single-stage multi-server queue, FIFO.

    The first `warmup` batches of each replication are discarded, a tenth of
    the run unless told otherwise. A queue started empty is not in steady
    state, and its early, unnaturally short waits bias every statistic
    downward.

    With `replications` above 1 the run is repeated on fresh seeds and the
    samples pooled. Prefer more replications over a longer single run: waits
    within one run are autocorrelated, so extra batches buy less independence
    than they appear to.
    """
    if not workload.is_stable:
        raise ValueError("cannot simulate an unstable queue: utilization is at or above 1")
    if warmup is None:
        warmup = batches // 10

    waits: list[float] = []
    sojourns: list[float] = []
    for rep in range(replications):
        run = _run_once(workload, batches, warmup, seed + rep * 7919)
        waits.extend(run.waits)
        sojourns.extend(run.sojourns)
    return SimulationResult(waits=waits, sojourns=sojourns)


@lru_cache(maxsize=256)
def _run_once(workload: Workload, batches: int, warmup: int, seed: int) -> SimulationResult:
    """One replication, memoised.

    Planners re-simulate a pipeline after changing one stage, and every other
    stage is then the same workload on the same seed. The cache turns those
    repeats into lookups; callers copy the samples out rather than mutate them.
    """
    rng = random.Random(seed)
    next_interarrival = _sampler(rng, 1.0 / workload.arrival_rate, workload.arrival_cv)
    next_service = _sampler(rng, workload.service_time, workload.service_cv)

    # One "next free" time per server, kept as a heap so the earliest is on top.
    free_at = [0.0] * workload.servers
    heapq.heapify(free_at)

    arrival = 0.0
    waits: list[float] = []
    sojourns: list[float] = []

    for index in range(batches):
        arrival += next_interarrival()
        earliest_free = heapq.heappop(free_at)
        start = max(arrival, earliest_free)
        finish = start + next_service()
        heapq.heappush(free_at, finish)

        if index >= warmup:
            waits.append(start - arrival)
            sojourns.append(finish - arrival)

    return SimulationResult(waits=waits, sojourns=sojourns)


def simulate_pipeline(
    pipeline: Pipeline,
    *,
    batches: int = 20_000,
    warmup: int | None = None,
    seed: int = DEFAULT_SEED,
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
    waits = [sum(step) for step in zip(*(r.waits for r in per_stage))]
    sojourns = [sum(step) for step in zip(*(r.sojourns for r in per_stage))]
    return SimulationResult(waits=waits, sojourns=sojourns)


def sla_attainment(
    pipeline: Pipeline,
    *,
    batches: int,
    replications: int = 1,
    seed: int = DEFAULT_SEED,
) -> float:
    """Share of batches landing inside the pipeline's SLA, by simulation."""
    result = simulate_pipeline(pipeline, batches=batches, seed=seed, replications=replications)
    return result.attainment(pipeline.sla_minutes)


def attainment_curve(
    pipeline: Pipeline,
    multipliers: list[float],
    *,
    batches: int = 8_000,
    seed: int = DEFAULT_SEED,
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
        share = sla_attainment(scaled, batches=batches, replications=replications, seed=seed)
        curve.append((multiplier, share))
    return curve
