"""Generate docs/ceiling.svg from the model.

Two panels. On the left, SLA attainment against fleet size, which shows the
ceiling capacity cannot pass. On the right, the utilization cliff at the
bottleneck. Both are computed by the same code that writes the memo.

    python docs/make_chart.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sla_capacity.economics import utilization_cliff  # noqa: E402
from sla_capacity.loader import load_pipeline  # noqa: E402
from sla_capacity.planning import attainment_ceiling  # noqa: E402
from sla_capacity.simulate import simulate_pipeline  # noqa: E402

WIDTH, HEIGHT = 1240, 600
PAD_T, PAD_B = 128, 96
GUTTER = 72
PANEL_W = (WIDTH - 96 - 96 - GUTTER) / 2
LEFT_X = 96
RIGHT_X = LEFT_X + PANEL_W + GUTTER

CHARCOAL = "#252525"
PARCHMENT = "#F1ECDC"
RED = "#E22D00"
MUTED = "rgba(241,236,220,0.55)"
GRID = "rgba(241,236,220,0.13)"

TARGET = 0.99
BATCHES = 6_000
REPLICATIONS = 4
SEED = 20260916


def main() -> None:
    pipeline = load_pipeline()
    base_fleet = sum(s.workload.servers for s in pipeline.stages)
    ceiling = attainment_ceiling(pipeline, batches=BATCHES, replications=REPLICATIONS)

    # Scale the whole fleet up and measure attainment at each size.
    points: list[tuple[int, float]] = []
    for multiple in (1, 2, 3, 4, 6, 8, 12):
        scaled = pipeline
        for stage in pipeline.stages:
            scaled = scaled.with_servers(stage.name, stage.workload.servers * multiple)
        result = simulate_pipeline(
            scaled, batches=BATCHES, warmup=BATCHES // 10, seed=SEED, replications=REPLICATIONS
        )
        points.append((base_fleet * multiple, result.attainment(pipeline.sla_minutes)))

    cliff = utilization_cliff(pipeline, pipeline.evaluate().bottleneck.name)

    parts: list[str] = []
    add = parts.append
    add(f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{HEIGHT}" '
        f'viewBox="0 0 {WIDTH} {HEIGHT}" font-family="Fira Sans, Segoe UI, sans-serif">')
    add(f'<rect width="{WIDTH}" height="{HEIGHT}" fill="{CHARCOAL}"/>')
    add(f'<text x="{LEFT_X}" y="40" fill="{RED}" font-size="13" font-weight="700" '
        f'letter-spacing="2">PIPELINE SLA CAPACITY</text>')
    add(f'<text x="{LEFT_X}" y="70" fill="{PARCHMENT}" font-size="25" font-weight="700">'
        f'The mean meets the SLA. The pipeline misses it 38% of the time.</text>')
    add(f'<text x="{LEFT_X}" y="94" fill="{MUTED}" font-size="13.5">'
        f'And no amount of capacity fixes that, because the service times alone '
        f'breach the promise.</text>')

    plot_h = HEIGHT - PAD_T - PAD_B

    # --- left panel: attainment against fleet size ---
    max_fleet = points[-1][0]

    def lx(fleet: int) -> float:
        return LEFT_X + (fleet - base_fleet) / (max_fleet - base_fleet) * PANEL_W

    def ly(share: float) -> float:
        return PAD_T + plot_h - share * plot_h

    add(f'<text x="{LEFT_X}" y="{PAD_T - 16}" fill="{PARCHMENT}" font-size="14" '
        f'font-weight="700">SLA attainment against fleet size</text>')
    for share in (0.0, 0.2, 0.4, 0.6, 0.8, 1.0):
        y = ly(share)
        add(f'<line x1="{LEFT_X}" y1="{y:.1f}" x2="{LEFT_X + PANEL_W:.1f}" y2="{y:.1f}" '
            f'stroke="{GRID}"/>')
        add(f'<text x="{LEFT_X - 10}" y="{y + 4:.1f}" fill="{MUTED}" font-size="11.5" '
            f'text-anchor="end">{share * 100:.0f}%</text>')

    # Target and ceiling
    add(f'<line x1="{LEFT_X}" y1="{ly(TARGET):.1f}" x2="{LEFT_X + PANEL_W:.1f}" '
        f'y2="{ly(TARGET):.1f}" stroke="{PARCHMENT}" stroke-width="1.5" '
        f'stroke-dasharray="6 4" stroke-opacity="0.7"/>')
    add(f'<text x="{LEFT_X + PANEL_W:.1f}" y="{ly(TARGET) - 8:.1f}" fill="{PARCHMENT}" '
        f'font-size="11.5" text-anchor="end">99% target</text>')

    cap = ceiling.attainment_ceiling
    add(f'<line x1="{LEFT_X}" y1="{ly(cap):.1f}" x2="{LEFT_X + PANEL_W:.1f}" '
        f'y2="{ly(cap):.1f}" stroke="{RED}" stroke-width="1.5" stroke-dasharray="4 4"/>')
    add(f'<rect x="{LEFT_X + PANEL_W - 168:.1f}" y="{ly(cap) + 8:.1f}" width="168" '
        f'height="21" fill="{RED}"/>')
    add(f'<text x="{LEFT_X + PANEL_W - 84:.1f}" y="{ly(cap) + 23:.1f}" fill="{PARCHMENT}" '
        f'font-size="11.5" font-weight="700" text-anchor="middle">'
        f'ceiling {cap * 100:.0f}%, unreachable</text>')

    polyline = " ".join(f"{lx(f):.1f},{ly(a):.1f}" for f, a in points)
    add(f'<polyline points="{polyline}" fill="none" stroke="{RED}" stroke-width="3" '
        f'stroke-linejoin="round"/>')
    for fleet, share in points:
        add(f'<circle cx="{lx(fleet):.1f}" cy="{ly(share):.1f}" r="4" fill="{PARCHMENT}"/>')

    for fleet, _ in points:
        add(f'<text x="{lx(fleet):.1f}" y="{PAD_T + plot_h + 22:.1f}" fill="{MUTED}" '
            f'font-size="11.5" text-anchor="middle">{fleet}</text>')
    add(f'<text x="{LEFT_X + PANEL_W / 2:.1f}" y="{PAD_T + plot_h + 46:.1f}" '
        f'fill="{MUTED}" font-size="12" text-anchor="middle">Total workers across '
        f'all four stages</text>')

    # --- right panel: the utilization cliff ---
    latencies = [r.latency_minutes for r in cliff]
    y_max = max(latencies) * 1.1

    def rx(index: int) -> float:
        return RIGHT_X + (index + 0.5) / len(cliff) * PANEL_W

    def ry(value: float) -> float:
        return PAD_T + plot_h - value / y_max * plot_h

    add(f'<text x="{RIGHT_X}" y="{PAD_T - 16}" fill="{PARCHMENT}" font-size="14" '
        f'font-weight="700">Latency at the bottleneck, by utilization</text>')
    for i in range(5):
        value = y_max * i / 4
        y = ry(value)
        add(f'<line x1="{RIGHT_X}" y1="{y:.1f}" x2="{RIGHT_X + PANEL_W:.1f}" y2="{y:.1f}" '
            f'stroke="{GRID}"/>')
        add(f'<text x="{RIGHT_X - 10}" y="{y + 4:.1f}" fill="{MUTED}" font-size="11.5" '
            f'text-anchor="end">{value:.0f}m</text>')

    bar_w = PANEL_W / len(cliff) * 0.62
    for i, row in enumerate(cliff):
        x = rx(i) - bar_w / 2
        y = ry(row.latency_minutes)
        hot = row.utilization >= 0.90
        add(f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w:.1f}" '
            f'height="{PAD_T + plot_h - y:.1f}" fill="{RED if hot else PARCHMENT}"/>')
        add(f'<text x="{rx(i):.1f}" y="{y - 7:.1f}" fill="{MUTED}" font-size="10.5" '
            f'text-anchor="middle">{row.relative_to_70:.1f}x</text>')
        add(f'<text x="{rx(i):.1f}" y="{PAD_T + plot_h + 22:.1f}" fill="{MUTED}" '
            f'font-size="11.5" text-anchor="middle">{row.utilization * 100:.0f}%</text>')
    add(f'<text x="{RIGHT_X + PANEL_W / 2:.1f}" y="{PAD_T + plot_h + 46:.1f}" '
        f'fill="{MUTED}" font-size="12" text-anchor="middle">'
        f'Utilization, indexed to 70% busy</text>')

    add(f'<text x="{LEFT_X}" y="{HEIGHT - 24}" fill="{MUTED}" font-size="11.5">'
        f'Attainment measured by discrete-event simulation with pooled replications. '
        f'Latency from Kingman&#8217;s approximation. Illustrative figures.</text>')
    add("</svg>")

    out = Path(__file__).parent / "ceiling.svg"
    out.write_text("\n".join(parts), encoding="utf-8")
    print(f"Wrote {out}")
    print(f"Ceiling {cap * 100:.1f}%; fleet sweep {points[0][1]*100:.0f}% at "
          f"{points[0][0]} workers to {points[-1][1]*100:.0f}% at {points[-1][0]}")


if __name__ == "__main__":
    main()
