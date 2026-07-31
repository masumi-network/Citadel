"""Cost model for a Citadel node, computed from measured resource use.

Every figure here is either measured on the running node or a list price fetched
from the vendor. Nothing is estimated from intuition, because the output of this
script is intended to back public claims and a claim that cannot be traced to a
measurement should not be made.

Inputs come from two places:

* Resource averages: Railway's own service metrics for the deployment. Read them
  with the Railway MCP (``service_metrics``, ``measurements=[CPU_USAGE,
  MEMORY_USAGE_GB, DISK_USAGE_GB, NETWORK_TX_GB]``) over at least 24h, and pass
  them below. A 1h window is not representative: this service idles near zero
  CPU and spikes during evolve passes.
* Prices: Railway's published pricing page. They are per-second, and they are
  arguments rather than constants so a price change is a one-line edit and shows
  up in the output provenance.

Usage:
    python scripts/bench/cost_model.py                       # measured defaults
    python scripts/bench/cost_model.py --memory-gb 2.0       # what-if
    python scripts/bench/cost_model.py --json
"""
from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass

SECONDS_PER_MONTH = 30 * 24 * 3600

# Railway list price, fetched from https://railway.com/pricing on 2026-07-31.
# Per second, which is also how they bill.
PRICE_VCPU_SECOND = 0.00000772
PRICE_GB_RAM_SECOND = 0.00000386
PRICE_GB_VOLUME_SECOND = 0.00000006
PRICE_EGRESS_GB = 0.05

# EVERY service in the Railway project, measured over a 24h window on
# 2026-07-31 via service_metrics. Averages, not peaks.
#
# The first version of this model covered only Citadel-Archive and reported
# $46.74. That was wrong by 18%: `list_services` shows three services, and a
# cost model that silently omits two of them is worse than no model, because it
# reads as complete. Whenever a service is added to the project it has to be
# added here, or this understates again in exactly the same way.
#
#   name, vCPU, memory GB, volume GB, egress GB/hour
MEASURED_SERVICES: tuple[tuple[str, float, float, float, float], ...] = (
    ("Citadel-Archive", 0.0864, 4.4398, 1.4376, 0.0102),
    ("Postgres", 0.0105, 0.7402, 1.8468, 0.0000),
    ("Citadel-GitHub-Sync", 0.0000, 0.0208, 1.0573, 0.0000),
)

# Measured by scripts/bench/search_bench.py against production on 2026-07-31.
MEASURED_SEARCH_P50_SECONDS = 0.2695


@dataclass
class CostLine:
    component: str
    basis: str
    monthly_usd: float
    share_pct: float


def service_cost(vcpu: float, memory_gb: float, volume_gb: float,
                 egress_gb_per_hour: float) -> dict[str, float]:
    return {
        "CPU": vcpu * PRICE_VCPU_SECOND * SECONDS_PER_MONTH,
        "Memory": memory_gb * PRICE_GB_RAM_SECOND * SECONDS_PER_MONTH,
        "Volume": volume_gb * PRICE_GB_VOLUME_SECOND * SECONDS_PER_MONTH,
        "Egress": egress_gb_per_hour * 720 * PRICE_EGRESS_GB,
    }


def monthly_cost(
    services: tuple[tuple[str, float, float, float, float], ...] = MEASURED_SERVICES,
) -> tuple[list[CostLine], dict[str, float]]:
    """Cost per component summed across EVERY service in the project.

    Returns the component breakdown and the per-service totals, because the two
    answer different questions: which resource to optimise, and which service is
    carrying the bill.
    """
    per_service: dict[str, float] = {}
    totals: dict[str, float] = {"CPU": 0.0, "Memory": 0.0, "Volume": 0.0, "Egress": 0.0}
    for name, vcpu, memory_gb, volume_gb, egress in services:
        costs = service_cost(vcpu, memory_gb, volume_gb, egress)
        per_service[name] = round(sum(costs.values()), 2)
        for component, value in costs.items():
            totals[component] += value

    grand = sum(totals.values()) or 1.0
    bases = {
        "CPU": f"{sum(s[1] for s in services):.4f} vCPU sustained",
        "Memory": f"{sum(s[2] for s in services):.2f} GB resident",
        "Volume": f"{sum(s[3] for s in services):.2f} GB on disk",
        "Egress": f"{sum(s[4] for s in services) * 720:.1f} GB/mo",
    }
    lines = [
        CostLine(component=name, basis=bases[name], monthly_usd=round(value, 2),
                 share_pct=round(100 * value / grand, 1))
        for name, value in totals.items()
    ]
    return lines, per_service


def marginal_search_cost(p50_seconds: float, vcpu_fraction: float) -> float:
    """Cost of serving one additional search.

    Embeddings are local (cognee is installed with the ``fastembed`` extra, model
    BAAI/bge-small-en-v1.5), and ``AUTO_FEEDBACK`` is forced off in
    kb/cognee_client.py, which removes the structured-output LLM call cognee
    otherwise runs before every retrieval. So a search buys CPU time and nothing
    else. There is no per-query API bill to add.
    """
    return p50_seconds * vcpu_fraction * PRICE_VCPU_SECOND


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--memory-gb",
        type=float,
        default=None,
        help="what-if: override the Citadel-Archive memory figure, the dominant line",
    )
    parser.add_argument("--search-p50-seconds", type=float, default=MEASURED_SEARCH_P50_SECONDS)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    services = MEASURED_SERVICES
    if args.memory_gb is not None:
        head = services[0]
        services = ((head[0], head[1], args.memory_gb, head[3], head[4]),) + services[1:]

    lines, per_service = monthly_cost(services)
    total = round(sum(line.monthly_usd for line in lines), 2)
    # Pessimistic: assumes a search saturates a full vCPU for its whole p50.
    # The measured CPU peak over 24h was 0.26 vCPU, so this over-states by ~4x
    # and is the number to quote.
    per_search = marginal_search_cost(args.search_p50_seconds, 1.0)

    if args.json:
        print(json.dumps({
            "monthly_total_usd": total,
            "services": per_service,
            "lines": [asdict(line) for line in lines],
            "marginal_cost_per_search_usd": round(per_search, 8),
            "marginal_cost_per_1k_searches_usd": round(per_search * 1000, 4),
            "searches_per_month_to_double_cost": int(total / per_search) if per_search else None,
            "price_source": "https://railway.com/pricing, fetched 2026-07-31",
        }, indent=2))
        return 0

    print("Citadel project: monthly cost from measured resource use")
    print("Prices: Railway list, fetched 2026-07-31. Usage: 24h averages.")
    print(f"Covers all {len(services)} services in the project.\n")
    for name, value in per_service.items():
        print(f"  {name:22} ${value:7.2f}")
    print()
    for line in lines:
        print(f"  {line.component:8} {line.basis:24} ${line.monthly_usd:7.2f}  {line.share_pct:5.1f}%")
    print(f"  {'':33}{'-' * 8}")
    print(f"  {'TOTAL':8} {'':24} ${total:7.2f}\n")

    dominant = max(lines, key=lambda line: line.monthly_usd)
    print(f"  {dominant.component} is {dominant.share_pct:.0f}% of the bill.")
    print("  The lever is resident footprint, not query volume.\n")
    print(f"  Marginal cost per search : ${per_search:.8f}")
    print(f"  Per 1,000 searches       : ${per_search * 1000:.4f}")
    if per_search:
        print(f"  Searches/month needed to double the bill: {int(total / per_search):,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
