"""Real-time MetadataChangeLog consumption.

Every other approach to "notice when data changed" polls. DataHub already publishes
every aspect mutation to Kafka as a `MetadataChangeLog`, so Lockout subscribes instead:
when an ingestion run touches a dataset Lockout has armed, the affected assertions are
re-evaluated immediately, and a model can be locked before anyone asks for a permit.

The wire format is Confluent-framed Avro, and the schema registry is *inside GMS* —
not on the conventional port 8081. On a quickstart install:

    http://localhost:8081/subjects                      -> connection refused
    http://localhost:8080/schema-registry/api/subjects  -> 200, 8 subjects

DataHub's own Actions tutorial points at :8081, which is why following it against
quickstart fails. Filed upstream — see docs/UPSTREAM_PRS.md.
"""

from __future__ import annotations

import io
import json
import logging
import struct
from dataclasses import dataclass
from typing import Any, Callable, Iterator

import fastavro
import requests

from lockout import config

logger = logging.getLogger(__name__)

TOPIC = "MetadataChangeLog_Versioned_v1"
SCHEMA_REGISTRY = f"{config.GMS_URL}/schema-registry/api"

_schema_cache: dict[int, Any] = {}


@dataclass
class MetadataChange:
    """One decoded aspect mutation."""

    entity_urn: str | None
    entity_type: str | None
    aspect_name: str | None
    change_type: str | None
    aspect: dict[str, Any] | None
    previous_aspect: dict[str, Any] | None
    actor: str | None

    @property
    def is_update(self) -> bool:
        """True when this replaced an existing value (vs. a first write)."""
        return self.previous_aspect is not None

    def changed(self) -> bool:
        return self.aspect != self.previous_aspect


def _fetch_schema(schema_id: int) -> Any:
    if schema_id not in _schema_cache:
        r = requests.get(f"{SCHEMA_REGISTRY}/schemas/ids/{schema_id}", timeout=15)
        r.raise_for_status()
        _schema_cache[schema_id] = fastavro.parse_schema(json.loads(r.json()["schema"]))
    return _schema_cache[schema_id]


def decode(payload: bytes) -> dict[str, Any]:
    """Decode a Confluent-framed Avro record.

    Wire format: 1 magic byte (0), then a 4-byte big-endian schema id, then the Avro
    body written without its own schema header.
    """
    magic, schema_id = struct.unpack(">bI", payload[:5])
    if magic != 0:
        raise ValueError(f"unexpected Confluent magic byte: {magic}")
    return fastavro.schemaless_reader(io.BytesIO(payload[5:]), _fetch_schema(schema_id))


def _as_json(value: Any) -> dict[str, Any] | None:
    """MCL carries aspect payloads as JSON *strings*, so no Avro work is needed to diff."""
    if not value:
        return None
    raw = value.get("value") if isinstance(value, dict) else value
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", "replace")
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {"_raw": raw}
    return raw if isinstance(raw, dict) else None


def to_change(record: dict[str, Any]) -> MetadataChange:
    created = record.get("created") or {}
    return MetadataChange(
        entity_urn=record.get("entityUrn"),
        entity_type=record.get("entityType"),
        aspect_name=record.get("aspectName"),
        change_type=record.get("changeType"),
        aspect=_as_json(record.get("aspect")),
        previous_aspect=_as_json(record.get("previousAspectValue")),
        actor=created.get("actor") if isinstance(created, dict) else None,
    )


def watch(
    interesting: Callable[[MetadataChange], bool] | None = None,
    bootstrap: str = "127.0.0.1:9092",
    group_id: str = "lockout-watcher",
    from_beginning: bool = False,
    timeout_s: float | None = None,
) -> Iterator[MetadataChange]:
    """Yield metadata changes as they happen.

    `interesting` is applied *before* any expensive work so that the common case — a
    change to an asset Lockout does not care about — costs one tuple comparison.
    """
    from confluent_kafka import Consumer  # imported here so `--help` works without kafka

    consumer = Consumer(
        {
            "bootstrap.servers": bootstrap,
            "group.id": group_id,
            "auto.offset.reset": "earliest" if from_beginning else "latest",
            "enable.auto.commit": True,
            # localhost resolves to ::1 first on macOS and the broker only listens on
            # IPv4, which produces a burst of alarming-but-harmless connection errors.
            "broker.address.family": "v4",
            "log_level": 0,
        }
    )
    consumer.subscribe([TOPIC])

    import time

    deadline = (time.time() + timeout_s) if timeout_s else None
    try:
        while True:
            if deadline and time.time() > deadline:
                return
            msg = consumer.poll(1.0)
            if msg is None:
                continue
            if msg.error():
                logger.warning("kafka error: %s", msg.error())
                continue
            try:
                change = to_change(decode(msg.value()))
            except Exception as exc:  # noqa: BLE001 — a bad record must not kill the watch
                logger.debug("undecodable MCL record: %s", exc)
                continue
            if interesting and not interesting(change):
                continue
            yield change
    finally:
        consumer.close()


def watches_datasets(urns: set[str]) -> Callable[[MetadataChange], bool]:
    """Pre-filter: only aspect changes on datasets Lockout has armed."""
    aspects = {
        "datasetProperties",
        "operation",
        "upstreamLineage",
        "schemaMetadata",
        "datasetProfile",
        "status",
    }

    def predicate(change: MetadataChange) -> bool:
        return (
            change.entity_type == "dataset"
            and change.entity_urn in urns
            and change.aspect_name in aspects
        )

    return predicate
