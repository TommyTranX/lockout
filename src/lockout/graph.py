"""DataHub client, emitter, and the small set of graph reads the gate depends on."""

from __future__ import annotations

import time
from functools import lru_cache
from typing import Any, Iterable

import requests
from datahub.emitter.mcp import MetadataChangeProposalWrapper
from datahub.ingestion.graph.client import DatahubClientConfig, DataHubGraph

from lockout import config


@lru_cache(maxsize=1)
def client() -> DataHubGraph:
    return DataHubGraph(
        DatahubClientConfig(server=config.GMS_URL, token=config.GMS_TOKEN)
    )


def emit(mcps: Iterable[MetadataChangeProposalWrapper]) -> int:
    g = client()
    n = 0
    for mcp in mcps:
        g.emit(mcp)
        n += 1
    return n


def gql(query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
    """Execute GraphQL against GMS.

    Used directly rather than through the SDK in two places where the SDK is broken
    against open-source GMS — see `writeback/assertions.py`.
    """
    r = requests.post(
        f"{config.GMS_URL}/api/graphql",
        json={"query": query, "variables": variables or {}},
        headers={
            "X-DataHub-Actor": "urn:li:corpuser:datahub",
            **({"Authorization": f"Bearer {config.GMS_TOKEN}"} if config.GMS_TOKEN else {}),
        },
        timeout=30,
    )
    payload = r.json()
    if payload.get("errors"):
        raise RuntimeError(f"GraphQL error: {payload['errors']}")
    return payload.get("data") or {}


# --------------------------------------------------------------------- lineage
_UPSTREAM_Q = """
query($urn:String!, $count:Int!){
  searchAcrossLineage(input:{urn:$urn, direction:UPSTREAM, query:"*", start:0, count:$count}){
    total
    searchResults{ degree entity{ urn type } }
  }
}
"""


def upstream_of(urn: str, count: int = 50) -> list[dict[str, Any]]:
    """Every entity upstream of `urn`, with its hop distance.

    This is the whole point of the project: a training job hands us only its own model
    URN and DataHub tells us, transitively, which datasets it ultimately depends on.
    """
    data = gql(_UPSTREAM_Q, {"urn": urn, "count": count})
    results = (data.get("searchAcrossLineage") or {}).get("searchResults", [])
    return [
        {
            "urn": r["entity"]["urn"],
            "type": r["entity"]["type"],
            "degree": r.get("degree"),
        }
        for r in results
    ]


def upstream_datasets(urn: str) -> list[dict[str, Any]]:
    """Upstream entities of type DATASET, nearest hop first."""
    hits = [h for h in upstream_of(urn) if h["type"] == "DATASET"]
    return sorted(hits, key=lambda h: h["degree"] or 99)


# ------------------------------------------------------------------ assertions
_DATASET_ASSERTIONS_Q = """
query($urn:String!){
  dataset(urn:$urn){
    assertions(start:0,count:50){
      total
      assertions{
        urn
        info{ type description customAssertion{ field{ path } logic } }
        runEvents(status:COMPLETE, limit:1){
          total failed succeeded
          runEvents{ timestampMillis result{ type nativeResults{ key value } } }
        }
      }
    }
  }
}
"""


def dataset_assertions(dataset_urn: str) -> list[dict[str, Any]]:
    """Assertions on a dataset, each with its most recent run result."""
    data = gql(_DATASET_ASSERTIONS_Q, {"urn": dataset_urn})
    node = (data.get("dataset") or {}).get("assertions") or {}
    out = []
    for a in node.get("assertions", []):
        info = a.get("info") or {}
        custom = info.get("customAssertion") or {}
        runs = a.get("runEvents") or {}
        events = runs.get("runEvents") or []
        latest = events[0] if events else None
        out.append(
            {
                "urn": a["urn"],
                "type": info.get("type"),
                "description": info.get("description"),
                "field": (custom.get("field") or {}).get("path"),
                "logic": custom.get("logic"),
                "failed": runs.get("failed", 0),
                "latest_result": (latest or {}).get("result", {}).get("type"),
                "native_results": {
                    nr["key"]: nr["value"]
                    for nr in ((latest or {}).get("result", {}) or {}).get(
                        "nativeResults", []
                    )
                    or []
                },
                "timestamp": (latest or {}).get("timestampMillis"),
            }
        )
    return out


def failing_assertions(dataset_urn: str) -> list[dict[str, Any]]:
    return [a for a in dataset_assertions(dataset_urn) if a["latest_result"] == "FAILURE"]


def wait_for(predicate, timeout_s: float = 60, interval_s: float = 2.0):
    """Poll until `predicate()` is truthy. DataHub indexes asynchronously; callers that
    need read-after-write must wait rather than assume."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval_s)
    return None
