"""Shared fixtures.

The scenario and the two expensive derived results are built once per session.
Several tests read the same ceiling and the same plan, and simulating them
again per test buys nothing but wall-clock.
"""

from __future__ import annotations

import pytest

from sla_capacity.loader import load_pipeline
from sla_capacity.planning import attainment_ceiling, plan_for_attainment


@pytest.fixture(scope="session")
def pipeline():
    return load_pipeline()


@pytest.fixture(scope="session")
def ceiling(pipeline):
    return attainment_ceiling(pipeline, batches=6_000, replications=3)


@pytest.fixture(scope="session")
def plan(pipeline):
    return plan_for_attainment(pipeline, target=0.99, max_additions=6, batches=4_000, replications=2)
