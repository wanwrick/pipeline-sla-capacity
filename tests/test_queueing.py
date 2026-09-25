"""Queueing formulas, checked against closed forms and known values.

The load-bearing tests are the ones that pin the analytic model to textbook
results, and the one that checks the simulation reproduces them. If those two
ever disagree, every number in the memo is suspect.
"""

from __future__ import annotations

import math
from dataclasses import replace

import pytest

from sla_capacity.queueing import (
    Workload,
    erlang_c,
    kingman_wait_time,
    mmc_wait_time,
    queue_length,
    servers_for_target_latency,
    sojourn_time,
    utilization_for_target_latency,
    vut_factors,
    work_in_progress,
)


def workload(rho: float, servers: int = 1, service_time: float = 2.0, **kwargs) -> Workload:
    """Build a workload at a chosen utilization."""
    return Workload(
        arrival_rate=rho * servers / service_time,
        service_time=service_time,
        servers=servers,
        **kwargs,
    )


# --- basic properties ----------------------------------------------------------


def test_utilization_is_load_over_servers():
    w = Workload(arrival_rate=1.0, service_time=2.0, servers=4)
    assert w.offered_load == pytest.approx(2.0)
    assert w.utilization == pytest.approx(0.5)


def test_a_saturated_queue_is_unstable():
    assert not Workload(arrival_rate=1.0, service_time=2.0, servers=2).is_stable


def test_minimum_servers_for_stability():
    w = Workload(arrival_rate=1.0, service_time=2.0, servers=1)
    assert w.servers_needed_for_stability == 3  # load is 2.0, so 3 servers


def test_invalid_inputs_are_rejected():
    with pytest.raises(ValueError):
        Workload(arrival_rate=0.0, service_time=1.0, servers=1)
    with pytest.raises(ValueError):
        Workload(arrival_rate=1.0, service_time=1.0, servers=0)
    with pytest.raises(ValueError):
        Workload(arrival_rate=1.0, service_time=1.0, servers=1, service_cv=-0.5)


# --- Erlang C ------------------------------------------------------------------


def test_erlang_c_is_one_when_saturated():
    assert erlang_c(2, 2.0) == 1.0


def test_erlang_c_is_zero_with_no_load():
    assert erlang_c(3, 0.0) == 0.0


def test_erlang_c_single_server_equals_utilization():
    """For M/M/1 the chance of waiting is exactly rho."""
    for rho in (0.3, 0.5, 0.8):
        assert erlang_c(1, rho) == pytest.approx(rho, abs=1e-9)


def test_erlang_c_falls_as_servers_are_added():
    values = [erlang_c(c, 3.0) for c in (4, 5, 6, 8)]
    assert all(b < a for a, b in zip(values, values[1:]))


# --- M/M/c exact ----------------------------------------------------------------


def test_mm1_wait_matches_the_closed_form():
    """Wq = rho / (mu * (1 - rho)). At rho 0.7, ts 2.0: 0.7/(0.5*0.3) = 4.667."""
    assert mmc_wait_time(workload(0.7)) == pytest.approx(4.6667, abs=1e-3)


def test_mm1_sojourn_matches_the_closed_form():
    """W = 1 / (mu - lambda). At rho 0.7, ts 2.0: 1/(0.5-0.35) = 6.667."""
    assert sojourn_time(workload(0.7), exact_markovian=True) == pytest.approx(6.6667, abs=1e-3)


def test_an_unstable_queue_waits_forever():
    assert math.isinf(mmc_wait_time(Workload(arrival_rate=1.0, service_time=2.0, servers=2)))


def test_adding_servers_reduces_the_wait():
    waits = [mmc_wait_time(workload(0.8, servers=c)) for c in (2, 3, 4, 6)]
    assert all(b < a for a, b in zip(waits, waits[1:]))


# --- Kingman ---------------------------------------------------------------------


def test_kingman_reduces_to_the_exact_form_for_mm1():
    """With ca = cs = 1 the V term is 1 and Kingman is exact for a single server."""
    for rho in (0.3, 0.6, 0.85):
        w = workload(rho)
        assert kingman_wait_time(w) == pytest.approx(mmc_wait_time(w), rel=1e-9)


def test_kingman_is_close_to_exact_for_multiple_servers():
    """An approximation, so a few percent is expected and anything more is not."""
    for rho, c in ((0.7, 3), (0.8, 4), (0.85, 5)):
        w = workload(rho, servers=c)
        assert kingman_wait_time(w) == pytest.approx(mmc_wait_time(w), rel=0.12)


def test_zero_variability_means_zero_wait_in_the_formula():
    """A perfectly regular arrival meeting a perfectly regular service never queues."""
    w = workload(0.9, arrival_cv=0.0, service_cv=0.0)
    assert kingman_wait_time(w) == pytest.approx(0.0)


def test_doubling_variability_doubles_the_wait():
    """V enters linearly. This is why it is the cheapest lever."""
    base = workload(0.8, arrival_cv=1.0, service_cv=1.0)
    doubled = workload(0.8, arrival_cv=math.sqrt(2), service_cv=math.sqrt(2))
    assert kingman_wait_time(doubled) == pytest.approx(2 * kingman_wait_time(base), rel=1e-9)


def test_the_utilization_term_explodes_near_saturation():
    """The whole argument against running a pipeline hot."""
    at_70 = kingman_wait_time(workload(0.70))
    at_90 = kingman_wait_time(workload(0.90))
    at_95 = kingman_wait_time(workload(0.95))
    assert at_90 / at_70 == pytest.approx(3.857, rel=0.01)
    assert at_95 / at_90 > 2.0


def test_vut_factors_report_the_named_quantities():
    """V is the mean of the squared coefficients of variation; T is the service time."""
    w = workload(0.8, servers=3, arrival_cv=1.2, service_cv=0.9)
    v, u, t = vut_factors(w)
    assert v == pytest.approx((1.2**2 + 0.9**2) / 2)
    assert t == w.service_time
    assert u > 0


def test_kingman_is_infinite_for_an_unstable_queue():
    """Even with no variability at all: the guard comes before the arithmetic."""
    w = workload(1.0, servers=2, arrival_cv=0.0, service_cv=0.0)
    assert math.isinf(kingman_wait_time(w))


# --- Little's Law -------------------------------------------------------------------


def test_queue_length_is_arrival_rate_times_wait():
    w = workload(0.8, servers=3)
    assert queue_length(w) == pytest.approx(w.arrival_rate * kingman_wait_time(w))


def test_work_in_progress_exceeds_queue_length_by_the_jobs_in_service():
    """L = Lq + rho*c, the standard identity."""
    w = workload(0.8, servers=3)
    assert work_in_progress(w) - queue_length(w) == pytest.approx(w.offered_load)


# --- sizing ---------------------------------------------------------------------------


def test_sizing_finds_a_server_count_that_meets_the_target():
    w = workload(0.9, servers=2)
    needed = servers_for_target_latency(w, 4.0)
    assert needed is not None
    assert sojourn_time(replace(w, servers=needed)) <= 4.0


def test_sizing_returns_none_below_the_service_time():
    """No amount of parallelism makes one batch finish faster than it runs."""
    assert servers_for_target_latency(workload(0.5, service_time=2.0), 1.5) is None


def test_target_utilization_hits_the_latency_it_promises():
    w = workload(0.8, servers=3)
    target = 6.0
    rho = utilization_for_target_latency(w, target)
    assert rho is not None
    assert sojourn_time(w.at_utilization(rho)) == pytest.approx(target, rel=1e-3)


def test_target_utilization_is_none_below_the_service_time():
    assert utilization_for_target_latency(workload(0.5, service_time=2.0), 1.0) is None
