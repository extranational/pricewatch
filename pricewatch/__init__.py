"""pricewatch — detect OpenRouter endpoint price/discount changes for models a job depends on.

Two consumers, one registry:

* ``pricewatch check --job <name>``  — step-0 preflight. Exit 0 when every watched pin still
  matches its accepted baseline; exit 1 (HOLD) when it does not. Jobs call this before spending.
* ``pricewatch sweep``               — periodic sweep over every watch, for cron/human alerting.

The registry (``registry.json``) is the single source of truth: a model is watched only if it
has an entry. ``pricewatch accept`` is the human act that re-baselines after a deliberate change.

Stdlib only, on purpose: this has to run inside job containers with no dependency surface.
"""
from .catalog import Drift, Unverifiable, fetch_endpoints, find_endpoint, snapshot
from .registry import Registry, Watch

__all__ = [
    "Drift",
    "Unverifiable",
    "fetch_endpoints",
    "find_endpoint",
    "snapshot",
    "Registry",
    "Watch",
]
