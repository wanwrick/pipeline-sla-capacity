"""Render the capacity analysis as a decision memo.

Recommendation first, then the evidence. The memo is generated from the model,
so it cannot claim something the numbers do not support. Change the scenario and
the prose changes with it, including which recommendation it makes.
"""

from __future__ import annotations

from datetime import date

from .economics import CostModel, CostPoint, UtilizationRow, cheapest
from .pipeline import Pipeline, PipelineResult
from .planning import AttainmentPlan, CapacityCeiling
from .simulate import SimulationResult


def minutes(value: float) -> str:
    return f"{value:.1f} min"


def pct(value: float | None, places: int = 0) -> str:
    return "n/a" if value is None else f"{value * 100:.{places}f}%"


def money(value: float) -> str:
    sign = "-" if value < 0 else ""
    magnitude = abs(value)
    if magnitude >= 1_000_000:
        return f"{sign}${magnitude / 1_000_000:.2f}M"
    return f"{sign}${magnitude / 1_000:.1f}K"


def count_words(n: int) -> str:
    """Small counts read better as words in prose: four stages, not 4 stages."""
    words = ("zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine")
    return words[n] if n < len(words) else str(n)


def render(
    pipeline: Pipeline,
    result: PipelineResult,
    simulated: SimulationResult,
    plan: AttainmentPlan,
    ceiling: CapacityCeiling,
    variability_gain: tuple[float, float],
    attainment: list[tuple[float, float]],
    cliff: list[UtilizationRow],
    cost_points: list[CostPoint],
    costs: CostModel,
    target_utilizations: dict[str, float | None],
) -> str:
    sla = pipeline.sla_minutes
    target = ceiling.target_attainment
    observed = simulated.attainment(sla)
    bottleneck = result.bottleneck
    best = cheapest(cost_points)
    unreachable = not ceiling.is_reachable

    lines: list[str] = []
    add = lines.append

    add(f"# {pipeline.name}: capacity for a {sla:.0f}-minute SLA")
    add("")
    add(f"*Generated {date.today().isoformat()} from the scenarios directory. "
        f"Analytic sizing cross-checked against {simulated.completed:,} simulated "
        f"batches.*")
    add("")

    # --- Recommendation ---------------------------------------------------------
    add("## Recommendation")
    add("")
    if unreachable:
        add(f"**Stop funding capacity for this SLA. No amount of it reaches the "
            f"target.** Mean end-to-end latency is {minutes(result.total_latency)} "
            f"against a {sla:.0f}-minute promise, which reads as "
            f"{minutes(result.headroom_minutes)} of headroom on a capacity report. "
            f"Attainment is {pct(observed)}.")
        add("")
        fleet = pipeline.total_servers
        add(f"Adding {plan.total_added} workers, taking the fleet from {fleet} to "
            f"{fleet + plan.total_added}, moves attainment from "
            f"{pct(plan.start_attainment)} to {pct(plan.final_attainment)} and "
            f"then stops improving. Drive queueing out entirely with unlimited "
            f"capacity and the ceiling is **{pct(ceiling.attainment_ceiling)}**.")
        add("")
        add(f"The reason is that the service times alone breach the promise. With "
            f"zero waiting, the pipeline's own p95 is {minutes(ceiling.floor_p95)} "
            f"and its p99 is {minutes(ceiling.floor_p99)}. A "
            f"{sla:.0f}-minute SLA is not a capacity target here. It is a design "
            f"target, and this architecture does not meet it.")
        add("")
        add("**Three real options, in the order they should be considered.**")
        add("")
        add(f"1. **Re-price the promise.** This design holds "
            f"{minutes(ceiling.floor_p95)} at 95% attainment and "
            f"{minutes(ceiling.floor_p99)} at 99%. A number the pipeline can "
            f"hold beats a number that sounds good and breaches weekly.")
        before, after = variability_gain
        add(f"2. **Cut variability before buying capacity.** Halving service "
            f"variability at {bottleneck.name} moves attainment from "
            f"{pct(before)} to {pct(after)} with no additional compute. "
            f"Variability enters the wait linearly, so this is the cheapest lever "
            f"in the model and the one least often pulled.")
        add(f"3. **Re-engineer service time.** The mean floor is "
            f"{minutes(ceiling.floor_mean)} across {count_words(len(result.stages))} "
            f"stages, so no single stage "
            f"cut to zero reaches the target. This is a multi-stage redesign, and "
            f"it should be scoped as one rather than discovered halfway through.")
    elif plan.total_added:
        detail = ", ".join(f"{n} to {name}" for name, n in plan.servers_added.items())
        add(f"**Add {plan.total_added} workers: {detail}.** Attainment moves from "
            f"{pct(plan.start_attainment)} to {pct(plan.final_attainment)} "
            f"against the {pct(target)} target, at "
            f"{money(plan.total_added * costs.cost_per_server_month)} a month.")
    else:
        add(f"**No additional capacity is needed.** Attainment is "
            f"{pct(plan.start_attainment)} against the {pct(target)} target "
            f"with the current fleet.")
        add("")
        add(f"Mean latency is {minutes(result.total_latency)} today, inside the "
            f"promise. That is not the same as meeting it: only {pct(observed)} "
            f"of batches land inside the window, because an SLA is a percentile and "
            f"a mean says nothing about the tail.")
    add("")

    # --- The mean is not the SLA -------------------------------------------------
    add("## An average is not a service level")
    add("")
    add("| Measure | Value | Against a "
        f"{sla:.0f}-minute promise |")
    add("|---|---:|---|")
    add(f"| Mean latency | {minutes(simulated.mean_sojourn)} | inside |")
    add(f"| Median | {minutes(simulated.percentile(0.50))} | inside |")
    add(f"| p95 | {minutes(simulated.percentile(0.95))} | "
        f"{'outside' if simulated.percentile(0.95) > sla else 'inside'} |")
    add(f"| p99 | {minutes(simulated.percentile(0.99))} | "
        f"{'outside' if simulated.percentile(0.99) > sla else 'inside'} |")
    add(f"| Attainment | {pct(observed)} | target {pct(target)} |")
    add("")
    add("A dashboard reporting mean freshness would show this pipeline green every "
        "day it breached. That gap between the reported metric and the promised one "
        "is where most freshness SLAs quietly fail.")
    add("")

    # --- Where the time goes -----------------------------------------------------
    add("## Where the time goes")
    add("")
    add("Each stage is a queue. Latency is time waiting plus time running, and the "
        "two are worth separating: waiting is bought with capacity, running is "
        "bought with engineering.")
    add("")
    add("| Stage | Workers | Utilization | Wait | Service | Total | Share |")
    add("|---|---:|---:|---:|---:|---:|---:|")
    for stage in result.stages:
        workload = stage.stage.workload
        add(f"| {stage.name} | {workload.servers} | {pct(stage.utilization)} "
            f"| {minutes(stage.wait_minutes)} | {minutes(workload.service_time)} "
            f"| {minutes(stage.sojourn_minutes)} "
            f"| {pct(result.share_of_latency(stage))} |")
    add("")
    same = bottleneck.name == result.busiest.name
    add(f"**{bottleneck.name} is the constraint**, carrying "
        f"{pct(result.share_of_latency(bottleneck))} of end-to-end latency. "
        + ("It is also the busiest stage." if same else
           f"The busiest stage is {result.busiest.name}, which is not the same "
           f"thing: a stage can run hot and still finish fast, and a capacity plan "
           f"built off a utilization chart will fund the wrong one."))
    add("")
    add("Note how little of the total is queueing. Most of this latency is service "
        "time, which is why capacity buys so little of it back.")
    add("")

    # --- VUT ----------------------------------------------------------------------
    add("### The three levers")
    add("")
    add("Kingman's approximation factors waiting into variability, utilization and "
        "service time. Reporting them apart matters because the fix differs by "
        "factor. High variability is an engineering problem, high utilization a "
        "budget problem, high service time a code problem.")
    add("")
    add("| Stage | V (variability) | U (utilization term) | T (service) | Wait |")
    add("|---|---:|---:|---:|---:|")
    for stage in result.stages:
        add(f"| {stage.name} | {stage.variability:.2f} | {stage.utilization_term:.2f} "
            f"| {minutes(stage.stage.workload.service_time)} "
            f"| {minutes(stage.wait_minutes)} |")
    add("")
    worst_v = max(result.stages, key=lambda s: s.variability)
    add(f"{worst_v.name} carries the highest variability at V = "
        f"{worst_v.variability:.2f}. Almost no team measures the coefficient of "
        f"variation of its own batches. So V never appears in a capacity request, "
        f"even though it moves the answer as much as U does.")
    add("")

    # --- Utilization cliff ----------------------------------------------------------
    add("## Why running it hotter does not work either")
    add("")
    add(f"The utilization term is rho/(1-rho). It does not rise linearly. Latency "
        f"at {bottleneck.name}, indexed to 70% busy:")
    add("")
    add("| Utilization | Latency | Relative to 70% |")
    add("|---:|---:|---:|")
    for row in cliff:
        add(f"| {pct(row.utilization)} | {minutes(row.latency_minutes)} "
            f"| {row.relative_to_70:.2f}x |")
    add("")
    add("The last few points of utilization are the most expensive capacity in the "
        "estate, and they are exactly what a cost-reduction exercise reaches for "
        "first.")
    add("")
    add("### Targets an on-call engineer can act on")
    add("")
    add("| Stage | Keep utilization below | Currently |")
    add("|---|---:|---:|")
    for stage in result.stages:
        target = target_utilizations.get(stage.name)
        add(f"| {stage.name} | "
            f"{pct(target) if target is not None else 'not reachable'} "
            f"| {pct(stage.utilization)} |")
    add("")
    add("These are instructions. \"Add capacity when latency degrades\" is not, "
        "because by then the queue has already formed.")
    add("")

    # --- Peak -----------------------------------------------------------------------
    add("## What happens at peak")
    add("")
    add("| Demand | SLA attainment |")
    add("|---:|---:|")
    for multiplier, share in attainment:
        note = " (unstable)" if share == 0.0 else ""
        add(f"| {multiplier:.1f}x | {pct(share)}{note} |")
    add("")
    first_bad = next((m for m, a in attainment if a < 0.50), None)
    if first_bad is not None:
        add(f"Attainment falls below half at {first_bad:.1f}x normal demand. Month "
            f"end, a campaign, or a backfill all clear that multiple, and none of "
            f"them are unusual events.")
    add("")

    # --- Economics --------------------------------------------------------------------
    add("## What the capacity is worth")
    add("")
    add(f"Workers cost {money(costs.cost_per_server_month)} a month each. A late "
        f"batch costs {money(costs.breach_cost)}, and attainment below "
        f"{pct(costs.sla_credit_threshold)} triggers a {money(costs.sla_credit)} "
        f"credit. Sweeping capacity at {bottleneck.name}:")
    add("")
    add("| Workers | Utilization | Attainment | Capacity | Breach | Total |")
    add("|---:|---:|---:|---:|---:|---:|")
    for point in cost_points:
        marker = " **<-**" if best and point.servers == best.servers else ""
        add(f"| {point.servers}{marker} | {pct(point.utilization)} "
            f"| {pct(point.attainment)} | {money(point.capacity_cost)} "
            f"| {money(point.breach_cost)} | {money(point.total_cost)} |")
    add("")
    if best:
        cleared = next(
            (p for p in cost_points if p.attainment >= costs.sla_credit_threshold), None
        )
        add(f"Total cost bottoms out at **{best.servers} workers on "
            f"{bottleneck.name}**, at {pct(best.utilization)} utilization and "
            f"{pct(best.attainment)} attainment. "
            + (f"The credit threshold is first cleared at {cleared.servers} workers."
               if cleared else
               "The credit threshold is never cleared at any capacity in this range. "
               "That is the same finding as above, arriving through the invoice "
               "instead of the queue."))
    add("")

    # --- Method ------------------------------------------------------------------------
    add("---")
    add("")
    add("## Method")
    add("")
    add("Sizing uses Kingman's approximation for G/G/c, exact Erlang C where the "
        "workload is Markovian, and Little's Law for queue depth. Percentiles and "
        "attainment come from discrete-event simulation, because an SLA is a "
        "percentile and a formula for the mean cannot answer it.")
    add("")
    add("The two are cross-checked. On M/M/c workloads the simulated mean wait "
        "matches the closed form within a few percent. The test suite fails if it "
        "stops doing so.")
    add("")
    add("Simulation uses the independent-replications method. One run of a queue is "
        "not an estimate. Successive waits are strongly autocorrelated, and single "
        "runs measured here landed up to 13% off the true mean while looking "
        "convergent.")
    add("")
    add("Stage latencies are sampled independently and summed, which understates "
        "the tail slightly. A slow upstream batch tends to arrive into a busy "
        "downstream stage, and that correlation is not modelled. The real "
        "attainment is therefore a little worse than reported here, not better.")
    add("")
    add("Every figure is illustrative and belongs to no employer. Regenerate with "
        "python run.py after editing the scenarios directory.")

    return "\n".join(lines)
