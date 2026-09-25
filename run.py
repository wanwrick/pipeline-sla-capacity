"""Size a pipeline against its freshness SLA and write the memo.

    python run.py                 # writes output/capacity.md
    python run.py --summary       # prints the stage table and stops
    python run.py --fast          # fewer simulated batches, for a quick look
"""

from __future__ import annotations

import argparse
from pathlib import Path

from sla_capacity.economics import cost_curve, utilization_cliff
from sla_capacity.loader import (
    DEFAULT_SCENARIO,
    load_costs,
    load_peak_multipliers,
    load_pipeline,
    load_simulation_config,
)
from sla_capacity.pipeline import stage_target_utilizations
from sla_capacity.planning import (
    attainment_ceiling,
    plan_for_attainment,
    variability_alternative,
)
from sla_capacity.report import minutes, pct, render
from sla_capacity.simulate import attainment_curve, simulate_pipeline

OUTPUT = Path(__file__).resolve().parent / "output"
TARGET_ATTAINMENT = 0.99


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", type=Path, default=DEFAULT_SCENARIO)
    parser.add_argument("--out", type=Path, default=OUTPUT / "capacity.md")
    parser.add_argument("--summary", action="store_true")
    parser.add_argument("--fast", action="store_true")
    parser.add_argument("--target", type=float, default=TARGET_ATTAINMENT)
    args = parser.parse_args()

    pipeline = load_pipeline(args.scenario)
    costs = load_costs(args.scenario)
    config = load_simulation_config(args.scenario)
    result = pipeline.evaluate()

    if args.summary:
        print(f"{pipeline.name}  |  SLA {pipeline.sla_minutes:.0f} min")
        print(f"{'Stage':<22}{'Workers':>8}{'Util':>8}{'Wait':>8}{'Total':>8}{'Share':>8}")
        for stage in result.stages:
            print(
                f"{stage.name:<22}{stage.stage.workload.servers:>8}"
                f"{pct(stage.utilization):>8}{stage.wait_minutes:>7.1f}m"
                f"{stage.sojourn_minutes:>7.1f}m"
                f"{pct(result.share_of_latency(stage)):>8}"
            )
        print(f"\nEnd to end: {minutes(result.total_latency)} (mean only)")
        print(f"Bottleneck: {result.bottleneck.name}")
        print("Run without --summary for attainment, which is the number that matters.")
        return 0

    # Simulation budget. The fast path trades precision for a quick look, and
    # the exploratory steps run on half the budget of the baseline.
    batches = 4_000 if args.fast else config["batches"]
    reps = 2 if args.fast else 6
    seed = config["seed"]
    full = {"batches": batches, "replications": reps, "seed": seed}
    half = {"batches": max(3_000, batches // 2), "replications": max(2, reps // 2), "seed": seed}

    print("Simulating baseline...")
    simulated = simulate_pipeline(pipeline, **full)

    print("Checking whether capacity can reach the target at all...")
    ceiling = attainment_ceiling(pipeline, target_attainment=args.target, **full)

    print("Planning capacity additions...")
    plan = plan_for_attainment(
        pipeline, target=args.target, max_additions=12 if args.fast else 20, **half
    )

    bottleneck = result.bottleneck
    print("Pricing the variability alternative...")
    variability_gain = variability_alternative(
        pipeline, bottleneck.name, bottleneck.stage.workload.service_cv / 2.0, **half
    )

    print("Running the peak-load curve...")
    attainment = attainment_curve(pipeline, load_peak_multipliers(args.scenario), **half)

    print("Sweeping cost against capacity...")
    current = bottleneck.stage.workload.servers
    points = cost_curve(
        pipeline,
        costs,
        bottleneck.name,
        range(max(1, current - 1), current + 6),
        batches=max(3_000, batches // 3),
        seed=seed,
    )

    memo = render(
        pipeline,
        result,
        simulated,
        plan,
        ceiling,
        variability_gain,
        attainment,
        utilization_cliff(pipeline, bottleneck.name),
        points,
        costs,
        stage_target_utilizations(pipeline),
    )

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(memo + "\n", encoding="utf-8")

    print()
    print(f"Wrote {args.out}")
    print(f"Mean {minutes(result.total_latency)} against a "
          f"{pipeline.sla_minutes:.0f} min SLA. Attainment "
          f"{pct(simulated.attainment(pipeline.sla_minutes))}, "
          f"ceiling {pct(ceiling.attainment_ceiling)} at unlimited capacity.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
