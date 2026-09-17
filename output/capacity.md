# Customer 360 freshness: capacity for a 15-minute SLA

*Generated 2026-09-16 from the scenarios directory. Analytic sizing cross-checked against 108,000 simulated batches.*

## Recommendation

**Stop funding capacity for this SLA. No amount of it reaches the target.** Mean end-to-end latency is 14.1 min against a 15-minute promise, which reads as 0.9 min of headroom on a capacity report. Attainment is 64%.

Adding 20 workers, taking the fleet from 11 to 31, moves attainment from 63% to 79% and then stops improving. Drive queueing out entirely with unlimited capacity and the ceiling is **79%**.

The reason is that the service times alone breach the promise. With zero waiting, the pipeline's own p95 is 23.1 min and its p99 is 31.9 min. A 15-minute SLA is not a capacity target for this design. It is a design target, and the design does not meet it.

**Three real options, in the order they should be considered.**

1. **Re-price the promise.** This design holds 23.1 min at 95% attainment and 31.9 min at 99%. A number the pipeline can hold beats a number that sounds good and breaches weekly.
2. **Cut variability before buying capacity.** Halving service variability at Gold conform moves attainment from 63% to 66% with no additional compute. Variability enters the wait linearly, so this is the cheapest lever in the model and the one least often pulled.
3. **Re-engineer service time.** The mean floor is 11.0 min across four stages, so no single stage cut to zero reaches the target. This is a multi-stage redesign, and it should be scoped as one rather than discovered halfway through.

## An average is not a service level

| Measure | Value | Against a 15-minute promise |
|---|---:|---|
| Mean latency | 13.9 min | inside |
| Median | 12.5 min | inside |
| p95 | 27.6 min | outside |
| p99 | 36.9 min | outside |
| Attainment | 64% | target 99% |

A dashboard reporting mean freshness would show this pipeline green every day it breached. That gap between the reported metric and the promised one is where most freshness SLAs quietly fail.

## Where the time goes

Each stage is a queue. Latency is time waiting plus time running, and the two are worth separating: waiting is bought with capacity, running is bought with engineering.

| Stage | Workers | Utilization | Wait | Service | Total | Share |
|---|---:|---:|---:|---:|---:|---:|
| Bronze ingest | 2 | 38% | 0.4 min | 1.4 min | 1.8 min | 13% |
| Silver validation | 3 | 57% | 1.2 min | 3.1 min | 4.3 min | 31% |
| Gold conform | 4 | 63% | 1.1 min | 4.6 min | 5.7 min | 40% |
| Serving refresh | 2 | 52% | 0.3 min | 1.9 min | 2.2 min | 16% |

**Gold conform is the constraint**, carrying 40% of end-to-end latency. It is also the busiest stage.

Note how little of the total is queueing. Most of this latency is service time, which is why capacity buys so little of it back.

### The three levers

Kingman's approximation factors waiting into variability, utilization and service time. Reporting them apart matters because the fix differs by factor: high variability is an engineering problem, high utilization is a budget problem, and high service time is a code problem.

| Stage | V (variability) | U (utilization term) | T (service) | Wait |
|---|---:|---:|---:|---:|
| Bronze ingest | 1.53 | 0.20 | 1.4 min | 0.4 min |
| Silver validation | 1.45 | 0.27 | 3.1 min | 1.2 min |
| Gold conform | 0.91 | 0.25 | 4.6 min | 1.1 min |
| Serving refresh | 0.45 | 0.41 | 1.9 min | 0.3 min |

Bronze ingest carries the highest variability at V = 1.53. Almost no team measures the coefficient of variation of its own batches, which is why V never appears in a capacity request even though it moves the answer as much as U does.

## Why running it hotter does not work either

The utilization term is rho/(1-rho). It does not rise linearly. Latency at Gold conform, indexed to 70% busy:

| Utilization | Latency | Relative to 70% |
|---:|---:|---:|
| 50% | 5.1 min | 0.82x |
| 60% | 5.5 min | 0.88x |
| 70% | 6.2 min | 1.00x |
| 80% | 7.8 min | 1.26x |
| 85% | 9.5 min | 1.53x |
| 90% | 12.9 min | 2.08x |
| 95% | 23.2 min | 3.74x |
| 98% | 54.4 min | 8.77x |

The last few points of utilization are the most expensive capacity in the estate, and they are exactly what a cost-reduction exercise reaches for first.

### Targets an on-call engineer can act on

| Stage | Keep utilization below | Currently |
|---|---:|---:|
| Bronze ingest | 43% | 38% |
| Silver validation | 61% | 57% |
| Gold conform | 68% | 63% |
| Serving refresh | 60% | 52% |

These are instructions. "Add capacity when latency degrades" is not, because by then the queue has already formed.

## What happens at peak

| Demand | SLA attainment |
|---:|---:|
| 1.0x | 63% |
| 1.1x | 57% |
| 1.2x | 49% |
| 1.3x | 38% |
| 1.4x | 24% |
| 1.5x | 11% |

Attainment falls below half at 1.2x normal demand. Month end, a campaign, or a backfill all clear that multiple, and none of them are unusual events.

## What the capacity is worth

Workers cost $1.2K a month each. A late batch costs $0.0K, and attainment below 99% triggers a $12.0K credit. Sweeping capacity at Gold conform:

| Workers | Utilization | Attainment | Capacity | Breach | Total |
|---:|---:|---:|---:|---:|---:|
| 3 | 84% | 44% | $11.8K | $575.7K | $587.5K |
| 4 | 63% | 63% | $13.0K | $378.4K | $391.4K |
| 5 | 51% | 67% | $14.2K | $344.6K | $358.8K |
| 6 | 42% | 67% | $15.3K | $337.5K | $352.8K |
| 7 **<-** | 36% | 68% | $16.5K | $335.3K | $351.8K |
| 8 | 32% | 68% | $17.7K | $335.0K | $352.7K |
| 9 | 28% | 68% | $18.9K | $335.0K | $353.9K |

Total cost bottoms out at **7 workers on Gold conform**, at 36% utilization and 68% attainment. The credit threshold is never cleared at any capacity in this range, which is the same finding as above arriving through the invoice instead of through the queue.

---

## Method

Sizing uses Kingman's approximation for G/G/c, exact Erlang C where the workload is Markovian, and Little's Law for queue depth. Percentiles and attainment come from discrete-event simulation, because an SLA is a percentile and a formula for the mean cannot answer it.

The two are cross-checked. On M/M/c workloads the simulated mean wait matches the closed form within a few percent, and the test suite fails if it stops doing so.

Simulation uses the independent-replications method. One run of a queue is not an estimate: successive waits are strongly autocorrelated, and single runs measured here landed up to 13% off the true mean while looking convergent.

Stage latencies are sampled independently and summed, which understates the tail slightly. A slow upstream batch tends to arrive into a busy downstream stage, and that correlation is not modelled. The real attainment is therefore a little worse than reported here, not better.

Every figure is illustrative and belongs to no employer. Regenerate with python run.py after editing the scenarios directory.
