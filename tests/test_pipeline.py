"""Pipeline, simulation and planning.

The most important test in this file is the one that checks the simulation
reproduces the closed form. The whole repo rests on the analytic model sizing
the system and the simulation measuring attainment, and that division of labour
is only safe while the two agree on the statistic they both compute.
"""

from __future__ import annotations

import pytest

from sla_capacity.economics import cheapest, cost_curve, utilization_cliff
from sla_capacity.loader import load_costs, load_pipeline
from sla_capacity.pipeline import (
    cheapest_path_to_sla,
    consolidate,
    stage_target_utilizations,
)
from sla_capacity.planning import (
    achievable_sla,
    attainment_ceiling,
    plan_for_attainment,
    variability_alternative,
)
from sla_capacity.queueing import Workload, mmc_wait_time
from sla_capacity.report import render
from sla_capacity.simulate import attainment_curve, simulate_pipeline, simulate_stage


@pytest.fixture(scope="module")
def pipeline():
    return load_pipeline()


# --- the cross-check the whole repo depends on -----------------------------------


@pytest.mark.parametrize("servers,rho", [(1, 0.7), (3, 0.8), (5, 0.85)])
def test_simulation_reproduces_the_closed_form(servers, rho):
    """Pooled replications should land within a few percent of exact M/M/c.

    Single runs will not. Successive waits in a queue are autocorrelated, and
    one run of 100k batches was measured 13% off. Replications are the fix and
    this test is what keeps them in place.
    """
    w = Workload(arrival_rate=rho * servers / 2.0, service_time=2.0, servers=servers)
    exact = mmc_wait_time(w)
    simulated = simulate_stage(w, batches=25_000, warmup=2_500, replications=16).mean_wait
    assert simulated == pytest.approx(exact, rel=0.06)


def test_simulating_an_unstable_queue_raises():
    w = Workload(arrival_rate=1.0, service_time=2.0, servers=2)
    with pytest.raises(ValueError, match="unstable"):
        simulate_stage(w, batches=100)


def test_replications_are_reproducible():
    w = Workload(arrival_rate=0.3, service_time=2.0, servers=1)
    first = simulate_stage(w, batches=3_000, warmup=300, replications=3, seed=7)
    second = simulate_stage(w, batches=3_000, warmup=300, replications=3, seed=7)
    assert first.sojourns == second.sojourns


def test_more_replications_yield_more_samples():
    w = Workload(arrival_rate=0.3, service_time=2.0, servers=1)
    one = simulate_stage(w, batches=2_000, warmup=200, replications=1)
    four = simulate_stage(w, batches=2_000, warmup=200, replications=4)
    assert four.completed == 4 * one.completed


def test_zero_variability_produces_no_queue_in_simulation():
    """A metronome feeding a metronome never waits. A good sanity check on _sample."""
    w = Workload(
        arrival_rate=0.4, service_time=2.0, servers=1, arrival_cv=0.0, service_cv=0.0
    )
    assert simulate_stage(w, batches=2_000, warmup=200).mean_wait == pytest.approx(0.0, abs=1e-9)


# --- pipeline structure ------------------------------------------------------------


def test_scenario_loads_with_every_stage(pipeline):
    assert len(pipeline.stages) == 4
    assert pipeline.sla_minutes == 15.0


def test_every_stage_is_stable(pipeline):
    assert all(s.workload.is_stable for s in pipeline.stages)


def test_end_to_end_latency_is_the_sum_of_stages(pipeline):
    result = pipeline.evaluate()
    assert result.total_latency == pytest.approx(sum(s.sojourn_minutes for s in result.stages))


def test_shares_of_latency_sum_to_one(pipeline):
    result = pipeline.evaluate()
    assert sum(result.share_of_latency(s) for s in result.stages) == pytest.approx(1.0)


def test_bottleneck_is_the_slowest_stage_not_the_busiest(pipeline):
    """They coincide here, but the code must not assume they always do."""
    result = pipeline.evaluate()
    assert result.bottleneck.sojourn_minutes == max(s.sojourn_minutes for s in result.stages)
    assert result.busiest.utilization == max(s.utilization for s in result.stages)


def test_adding_servers_lowers_latency(pipeline):
    before = pipeline.evaluate().total_latency
    after = pipeline.with_servers("Gold conform", 6).evaluate().total_latency
    assert after < before


def test_peak_demand_raises_utilization_everywhere(pipeline):
    peaked = pipeline.with_arrival_multiplier(1.3)
    for base, peak in zip(pipeline.stages, peaked.stages):
        assert peak.workload.utilization > base.workload.utilization


# --- the headline finding -----------------------------------------------------------


def test_the_mean_meets_the_sla(pipeline):
    """Half of the finding: on paper this pipeline looks fine."""
    assert pipeline.evaluate().meets_sla


def test_attainment_does_not(pipeline):
    """The other half. If these two ever agree, the memo needs rewriting."""
    simulated = simulate_pipeline(pipeline, batches=6_000, warmup=600, replications=4)
    assert simulated.attainment(pipeline.sla_minutes) < 0.90


def test_capacity_alone_cannot_reach_the_target(pipeline):
    """The claim the recommendation rests on, asserted rather than asserted-in-prose."""
    ceiling = attainment_ceiling(pipeline, batches=6_000, replications=3)
    assert not ceiling.is_reachable
    assert ceiling.floor_p99 > pipeline.sla_minutes


def test_the_service_time_floor_is_the_sum_of_service_times(pipeline):
    """With queueing gone, the mean is just the sum of the stages' service times."""
    expected = sum(s.workload.service_time for s in pipeline.stages)
    ceiling = attainment_ceiling(pipeline, batches=6_000, replications=3)
    assert ceiling.floor_mean == pytest.approx(expected, rel=0.05)


def test_achievable_sla_is_worse_at_a_higher_attainment_target(pipeline):
    at_95 = achievable_sla(pipeline, 0.95, batches=5_000, replications=3)
    at_99 = achievable_sla(pipeline, 0.99, batches=5_000, replications=3)
    assert at_99 > at_95 > pipeline.sla_minutes


# --- planning -------------------------------------------------------------------------


def test_the_attainment_plan_improves_attainment(pipeline):
    plan = plan_for_attainment(
        pipeline, target=0.99, max_additions=6, batches=4_000, replications=2
    )
    assert plan.final_attainment > plan.start_attainment
    assert plan.total_added == len(plan.steps)


def test_the_attainment_plan_gives_up_rather_than_overspending(pipeline):
    """It must report failure, not keep adding workers that buy nothing."""
    plan = plan_for_attainment(
        pipeline, target=0.99, max_additions=6, batches=4_000, replications=2
    )
    assert not plan.reached_target


def test_planning_stops_immediately_when_the_target_is_already_met(pipeline):
    plan = plan_for_attainment(
        pipeline, target=0.10, max_additions=6, batches=3_000, replications=2
    )
    assert plan.total_added == 0
    assert plan.reached_target


def test_cutting_variability_improves_attainment_without_capacity(pipeline):
    before, after = variability_alternative(
        pipeline, "Gold conform", 0.4, batches=4_000, replications=3
    )
    assert after > before


def test_mean_based_planner_stops_when_the_mean_already_passes(pipeline):
    """Documents why planning.py exists: this planner is satisfied too early."""
    assert cheapest_path_to_sla(pipeline) == []


def test_consolidate_counts_additions_per_stage():
    from sla_capacity.pipeline import Addition

    plan = [
        Addition("a", 1, 1.0, 10.0),
        Addition("b", 1, 0.5, 9.5),
        Addition("a", 1, 0.3, 9.2),
    ]
    assert consolidate(plan) == {"a": 2, "b": 1}


# --- targets and peak -------------------------------------------------------------------


def test_stage_targets_are_below_one_where_they_exist(pipeline):
    for target in stage_target_utilizations(pipeline).values():
        if target is not None:
            assert 0.0 < target < 1.0


def test_attainment_falls_as_demand_rises(pipeline):
    curve = attainment_curve(pipeline, [1.0, 1.2, 1.4], batches=3_000, replications=2)
    values = [a for _, a in curve]
    assert all(b <= a for a, b in zip(values, values[1:]))


def test_unstable_demand_reports_zero_rather_than_raising(pipeline):
    curve = attainment_curve(pipeline, [4.0], batches=1_000, replications=1)
    assert curve == [(4.0, 0.0)]


# --- economics -----------------------------------------------------------------------------


def test_utilization_cliff_is_monotonic_and_indexed_to_seventy(pipeline):
    rows = utilization_cliff(pipeline, "Gold conform")
    latencies = [r.latency_minutes for r in rows]
    assert all(b > a for a, b in zip(latencies, latencies[1:]))
    at_70 = next(r for r in rows if abs(r.utilization - 0.70) < 1e-9)
    assert at_70.relative_to_70 == pytest.approx(1.0)


def test_the_cliff_is_steeper_at_the_top(pipeline):
    """70 to 80 should cost less than 90 to 95. This is the argument in one assert."""
    rows = {round(r.utilization, 2): r.latency_minutes for r in utilization_cliff(pipeline, "Gold conform")}
    assert rows[0.95] - rows[0.90] > rows[0.80] - rows[0.70]


def test_cost_curve_has_a_minimum(pipeline):
    costs = load_costs()
    points = cost_curve(pipeline, costs, "Gold conform", range(3, 8), batches=2_500)
    assert points
    assert cheapest(points) is not None


def test_breach_cost_falls_as_attainment_rises():
    costs = load_costs()
    assert costs.breach_cost_at(0.999) < costs.breach_cost_at(0.95)


def test_the_sla_credit_applies_below_the_threshold():
    costs = load_costs()
    just_under = costs.breach_cost_at(costs.sla_credit_threshold - 0.001)
    just_over = costs.breach_cost_at(costs.sla_credit_threshold + 0.001)
    assert just_under - just_over > costs.sla_credit * 0.9


# --- report ---------------------------------------------------------------------------------


def test_report_renders_and_leads_with_the_recommendation(pipeline):
    result = pipeline.evaluate()
    simulated = simulate_pipeline(pipeline, batches=3_000, warmup=300, replications=2)
    plan = plan_for_attainment(pipeline, 0.99, max_additions=3, batches=2_500, replications=2)
    ceiling = attainment_ceiling(pipeline, batches=3_000, replications=2)
    costs = load_costs()

    memo = render(
        pipeline,
        result,
        simulated,
        plan,
        ceiling,
        {0.95: 23.0, 0.99: 32.0},
        variability_alternative(pipeline, "Gold conform", 0.5, batches=2_500, replications=2),
        attainment_curve(pipeline, [1.0, 1.2], batches=2_500, replications=2),
        utilization_cliff(pipeline, "Gold conform"),
        cost_curve(pipeline, costs, "Gold conform", range(4, 7), batches=2_500),
        costs,
        stage_target_utilizations(pipeline),
    )

    assert memo.startswith("# Customer 360 freshness")
    assert memo.index("## Recommendation") < memo.index("## Where the time goes")
    assert "—" not in memo  # house style: no em dash
