"""Queueing primitives, applied to data pipelines rather than call centres.

A pipeline stage is a queue. Batches arrive, a pool of workers serves them, and
the time a batch spends in the system is what freshness actually measures. Once
that is accepted, the answer to "how much capacity do we need" stops being a
guess and becomes arithmetic.

The one fact worth carrying out of this module: waiting time does not rise
linearly with utilization, it rises with rho/(1-rho). Going from 80% to 90% busy
does not cost 12% more delay, it roughly doubles it. That is why a pipeline that
looks comfortable on a capacity report misses its SLA anyway.

Frameworks: Little's Law and the VUT equation, Operations Management
(Prof. Yao Cui, Cornell). Kingman's approximation for the general case.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace


@dataclass(frozen=True)
class Workload:
    """A stage's demand and capability, in consistent time units (minutes).

    `arrival_cv` and `service_cv` are coefficients of variation: standard
    deviation over mean. They are the V in the VUT equation and they are the
    input most teams never measure, which is why their capacity plans are
    optimistic. Exponential arrivals give cv = 1; a perfectly regular cron
    gives cv = 0.
    """

    arrival_rate: float        # batches per minute
    service_time: float        # mean minutes to process one batch, one worker
    servers: int               # parallel workers, tasks, or cluster slots
    arrival_cv: float = 1.0
    service_cv: float = 1.0

    def __post_init__(self) -> None:
        if self.arrival_rate <= 0:
            raise ValueError("arrival_rate must be positive")
        if self.service_time <= 0:
            raise ValueError("service_time must be positive")
        if self.servers < 1:
            raise ValueError("servers must be at least 1")
        for name in ("arrival_cv", "service_cv"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} cannot be negative")

    @property
    def service_rate(self) -> float:
        """Batches per minute one worker can complete."""
        return 1.0 / self.service_time

    @property
    def offered_load(self) -> float:
        """Erlangs: the number of workers kept busy on average."""
        return self.arrival_rate * self.service_time

    @property
    def utilization(self) -> float:
        """Rho. At or above 1 the queue grows without bound."""
        return self.offered_load / self.servers

    @property
    def is_stable(self) -> bool:
        return self.utilization < 1.0

    @property
    def servers_needed_for_stability(self) -> int:
        """The floor below which no amount of patience helps."""
        return math.floor(self.offered_load) + 1

    def at_utilization(self, rho: float) -> "Workload":
        """The same stage under the demand that would keep it rho busy."""
        return replace(self, arrival_rate=rho * self.servers / self.service_time)


def erlang_c(servers: int, offered_load: float) -> float:
    """Probability an arriving batch has to wait at all (Erlang C).

    Computed with a recurrence rather than factorials so it stays numerically
    sound at the server counts a real cluster runs at.
    """
    if offered_load >= servers:
        return 1.0
    if offered_load <= 0:
        return 0.0

    # Erlang B by recurrence: B(0, a) = 1, B(n, a) = a*B(n-1, a) / (n + a*B(n-1, a))
    b = 1.0
    for n in range(1, servers + 1):
        b = (offered_load * b) / (n + offered_load * b)

    rho = offered_load / servers
    denominator = 1.0 - rho + rho * b
    return b / denominator if denominator > 0 else 1.0


def mmc_wait_time(workload: Workload) -> float:
    """Mean time in queue for M/M/c, in minutes. Exact, not an approximation."""
    if not workload.is_stable:
        return math.inf
    c = erlang_c(workload.servers, workload.offered_load)
    spare_capacity = workload.servers * workload.service_rate - workload.arrival_rate
    return c / spare_capacity


def kingman_wait_time(workload: Workload) -> float:
    """Mean time in queue for G/G/c, by Kingman's approximation: V x U x T.

    The three factors are the whole story a capacity conversation needs.

      V  variability, (ca^2 + cs^2) / 2
      U  utilization, rho / (1 - rho), the term that explodes
      T  service time

    Halving variability halves the wait at any utilization. That is usually
    cheaper than buying servers, and it is the lever nobody reaches for because
    nobody measures V.
    """
    if not workload.is_stable:
        return math.inf
    variability, utilization_term, service_time = vut_factors(workload)
    return variability * utilization_term * service_time


def vut_factors(workload: Workload) -> tuple[float, float, float]:
    """The V, U and T of Kingman's formula, returned separately.

    Reported apart because the fix differs by factor. High V is an engineering
    problem, high U is a budget problem, and high T is a code problem.
    """
    variability = (workload.arrival_cv**2 + workload.service_cv**2) / 2.0
    rho = workload.utilization
    if rho >= 1.0:
        return variability, math.inf, workload.service_time
    if workload.servers == 1:
        utilization_term = rho / (1.0 - rho)
    else:
        # Standard multi-server correction to the single-server U term.
        exponent = math.sqrt(2.0 * (workload.servers + 1.0)) - 1.0
        utilization_term = rho**exponent / (workload.servers * (1.0 - rho))
    return variability, utilization_term, workload.service_time


def sojourn_time(workload: Workload, *, exact_markovian: bool = False) -> float:
    """Total time in the system: queue plus service. This is freshness."""
    wait = mmc_wait_time(workload) if exact_markovian else kingman_wait_time(workload)
    return wait + workload.service_time


def queue_length(workload: Workload) -> float:
    """Batches waiting, by Little's Law: L = lambda x W."""
    return workload.arrival_rate * kingman_wait_time(workload)


def work_in_progress(workload: Workload) -> float:
    """Batches in the system, waiting or being served. Little's Law again."""
    return workload.arrival_rate * sojourn_time(workload)


def servers_for_target_latency(
    workload: Workload, target_minutes: float, *, max_servers: int = 10_000
) -> int | None:
    """Smallest server count whose mean sojourn time meets the target.

    Returns None when the target is below the service time itself, because no
    amount of parallelism makes a single batch finish faster than it runs.
    """
    if target_minutes <= workload.service_time:
        return None

    for servers in range(workload.servers_needed_for_stability, max_servers + 1):
        candidate = replace(workload, servers=servers)
        if sojourn_time(candidate) <= target_minutes:
            return servers
    return None


def utilization_for_target_latency(
    workload: Workload, target_minutes: float
) -> float | None:
    """The utilization at which the mean sojourn time equals the target.

    The number to put on a capacity dashboard. "Keep this stage under 78% busy"
    is an instruction an on-call engineer can act on at 3am. "Add capacity when
    latency degrades" is not.
    """
    if target_minutes <= workload.service_time:
        return None

    low, high = 1e-6, 0.999999
    for _ in range(200):
        mid = (low + high) / 2.0
        candidate = workload.at_utilization(mid)
        if sojourn_time(candidate) > target_minutes:
            high = mid
        else:
            low = mid
    return (low + high) / 2.0
