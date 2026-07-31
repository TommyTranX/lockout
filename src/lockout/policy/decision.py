"""The gate.

A training job asks Lockout for a permit and passes **only its own model URN**. It does
not know which tables it depends on, and it never names the stale one. Lockout answers
by walking the graph:

    mlModel ──Consumes──▶ mlFeature ──DerivedFrom──▶ dataset ──Upstream──▶ dataset
       │                      │                         │
       │                      │                         └─ failing assertions?
       │                      └─ which column does this feature read?
       └─ deny, and name the path that caused it

This is what separates a safety interlock from `if stale: deny`. The blocking fact can
sit three hops away from anything the job knows about, and the denial has to be able to
prove where it came from.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from lockout import config, graph, urns

Verdict = Literal["GRANTED", "DENIED"]


@dataclass
class Evidence:
    """One reason a permit was denied, with everything needed to audit it."""

    assertion_urn: str
    rule: str
    dataset_urn: str
    column: str | None
    description: str
    observed: dict[str, str]
    lineage_path: list[str]
    hops: int
    features_affected: list[str] = field(default_factory=list)
    transform_sql: str | None = None

    def summary(self) -> str:
        where = f"{urns.table_of(self.dataset_urn)}.{self.column}" if self.column else urns.table_of(self.dataset_urn)
        return f"{self.rule} failed on {where} ({self.hops} hops upstream)"


@dataclass
class Permit:
    verdict: Verdict
    model_urn: str
    decided_at_ms: int
    evidence: list[Evidence]
    upstream_datasets: list[str]
    features_checked: list[str]
    elapsed_ms: int

    @property
    def granted(self) -> bool:
        return self.verdict == "GRANTED"

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "model_urn": self.model_urn,
            "decided_at_ms": self.decided_at_ms,
            "elapsed_ms": self.elapsed_ms,
            "upstream_datasets": self.upstream_datasets,
            "features_checked": self.features_checked,
            "evidence": [asdict(e) for e in self.evidence],
        }

    def render(self) -> str:
        lines: list[str] = []
        if self.granted:
            lines.append(f"PERMIT GRANTED  for {self.model_urn}")
            lines.append(
                f"  checked {len(self.features_checked)} features across "
                f"{len(self.upstream_datasets)} upstream datasets — no failing assertions"
            )
            lines.append(f"  decided in {self.elapsed_ms} ms")
            return "\n".join(lines)

        lines.append(f"PERMIT DENIED   for {self.model_urn}")
        lines.append("")
        lines.append("  The training job did not name a table. It named itself.")
        lines.append("  Lockout resolved its features to source datasets and walked upstream:")
        lines.append("")
        for e in self.evidence:
            lines.append(f"  ✗ {e.summary()}")
            lines.append(f"      {e.description}")
            if e.observed:
                obs = "  ".join(f"{k}={v}" for k, v in e.observed.items())
                lines.append(f"      observed: {obs}")
            lines.append("      lineage path:")
            for i, hop in enumerate(e.lineage_path):
                lines.append(f"        {'  ' * i}{'└─▶ ' if i else ''}{hop}")
            if e.features_affected:
                lines.append(f"      features affected: {', '.join(e.features_affected)}")
            if e.transform_sql:
                first = e.transform_sql.strip().splitlines()[0]
                lines.append(f"      transform: {first} ...")
            lines.append(f"      assertion: {e.assertion_urn}")
            lines.append("")
        lines.append(f"  decided in {self.elapsed_ms} ms")
        return "\n".join(lines)


# ------------------------------------------------------------------ graph reads
# Note: `MLModelProperties.deployments` exists as an *aspect* field but is not exposed
# on the GraphQL type in OSS v1.5.0.6 ("Field 'deployments' in type 'MLModelProperties'
# is undefined"), so it is deliberately absent here.
_FEATURES_Q = """
query($urn:String!){
  mlModel(urn:$urn){
    urn
    properties{ mlFeatures }
  }
}
"""

_FEATURE_DETAIL_Q = """
query($urn:String!){
  mlFeature(urn:$urn){
    urn
    properties{ description sources{ urn } customProperties{ key value } }
  }
}
"""


def model_features(model_urn: str) -> list[str]:
    data = graph.gql(_FEATURES_Q, {"urn": model_urn})
    props = ((data.get("mlModel") or {}).get("properties") or {})
    return list(props.get("mlFeatures") or [])


def feature_detail(feature_urn: str) -> dict[str, Any]:
    data = graph.gql(_FEATURE_DETAIL_Q, {"urn": feature_urn})
    props = ((data.get("mlFeature") or {}).get("properties") or {})
    custom = {c["key"]: c["value"] for c in (props.get("customProperties") or [])}
    return {
        "urn": feature_urn,
        "sources": [s["urn"] for s in (props.get("sources") or [])],
        "column": custom.get("lockout.source_column"),
        "table": custom.get("lockout.source_table"),
    }


# ------------------------------------------------------------------- the gate
def request_permit(model_urn: str | None = None) -> Permit:
    """Decide whether `model_urn` may train right now."""
    started = time.time()
    model_urn = model_urn or urns.model()

    # 1. What does this model consume? (graph, not configuration)
    features = model_features(model_urn)
    details = [feature_detail(f) for f in features]

    # 2. Which datasets do those features derive from, and what is upstream of those?
    #    `upstream_datasets` is a transitive lineage query — this is the hop that makes
    #    the decision non-trivial, because the failure can live further up than any
    #    dataset the features directly name.
    direct_sources: set[str] = set()
    for d in details:
        direct_sources.update(d["sources"])

    reachable: dict[str, int] = {}
    for src in direct_sources:
        reachable.setdefault(src, 1)
        for hit in graph.upstream_datasets(src):
            hops = (hit.get("degree") or 1) + 1
            reachable[hit["urn"]] = min(reachable.get(hit["urn"], 99), hops)

    # 3. Are any assertions failing anywhere on that reachable set?
    evidence: list[Evidence] = []
    for dataset_urn, hops in sorted(reachable.items(), key=lambda kv: kv[1]):
        for a in graph.failing_assertions(dataset_urn):
            affected = [
                d["urn"].split(",")[-1].rstrip(")")
                for d in details
                if d["table"]
                and urns.dataset(d["table"]) == dataset_urn
                and (a["field"] is None or d["column"] == a["field"])
            ]
            evidence.append(
                Evidence(
                    assertion_urn=a["urn"],
                    rule=(a["type"] or "ASSERTION"),
                    dataset_urn=dataset_urn,
                    column=a["field"],
                    description=a["description"] or "",
                    observed=a["native_results"],
                    lineage_path=_path_to(model_urn, dataset_urn, details),
                    hops=hops,
                    features_affected=affected,
                )
            )

    elapsed = int((time.time() - started) * 1000)
    return Permit(
        verdict="DENIED" if evidence else "GRANTED",
        model_urn=model_urn,
        decided_at_ms=int(time.time() * 1000),
        evidence=evidence,
        upstream_datasets=sorted(reachable),
        features_checked=features,
        elapsed_ms=elapsed,
    )


def _path_to(model_urn: str, dataset_urn: str, details: list[dict[str, Any]]) -> list[str]:
    """A human-readable model -> feature -> dataset chain for the denial message."""
    short_model = model_urn.split(",")[-2] if "," in model_urn else model_urn
    path = [f"mlModel:{short_model}"]
    for d in details:
        if d["table"] and urns.dataset(d["table"]) == dataset_urn:
            path.append(f"mlFeature:{d['urn'].split(',')[-1].rstrip(')')}")
            break
    table = urns.table_of(dataset_urn)
    path.append(f"dataset:{table}")
    return path
