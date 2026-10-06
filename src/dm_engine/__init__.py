"""Outbound DM Engine — channel-agnostic, variant-instrumented outreach queue.

Design and contracts: docs/PLAN.md. This package deliberately has no third-party
runtime dependencies and contains NO send code (PLAN.md §14 non-goal 1).

Import the submodules directly (`from dm_engine import db, config, locks`); nothing
is re-exported here, so module import order stays obvious and cycle-free.
"""

__version__ = "0.1.0"
