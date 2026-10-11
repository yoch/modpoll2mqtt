"""Strict command decoding and bounded, process-local request deduplication."""

from __future__ import annotations

import hashlib
import json
import math
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import cast

from .types import ModbusValue, is_modbus_value_map


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> object:
    raise ValueError(f"Non-finite number: {value}")


def decode_json(payload: bytes, limit: int = 65536) -> dict[str, object]:
    if len(payload) > limit:
        raise ValueError("Command payload exceeds size limit")
    try:
        result: object = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise ValueError("Invalid UTF-8/JSON command") from exc
    if not isinstance(result, dict):
        raise ValueError("Command must be an object")
    return cast(dict[str, object], result)


@dataclass(frozen=True)
class Command:
    kind: str
    values: dict[str, ModbusValue]
    request_id: str | None = None
    expires_at: float | None = None


def parse_command(payload: bytes, kind: str, limit: int, ttl: float) -> Command:
    obj = decode_json(payload, limit)
    if kind != "command":
        if not is_modbus_value_map(obj):
            raise ValueError("Unsupported reference values")
        if any(isinstance(v, float) and not math.isfinite(v) for v in obj.values()):
            raise ValueError("Values must be finite")
        return Command(kind, obj)
    if set(obj) - {
        "version",
        "request_id",
        "operation",
        "values",
        "references",
        "expires_at",
    }:
        raise ValueError("Unknown command envelope field")
    identifier, operation = obj.get("request_id"), obj.get("operation")
    if type(obj.get("version")) is not int or obj["version"] != 1:
        raise ValueError("Unsupported command version")
    if not isinstance(identifier, str) or not 1 <= len(identifier) <= 128:
        raise ValueError("request_id must be a string of 1..128 characters")
    now = time.time()
    expires = obj.get("expires_at", now + ttl)
    if (
        isinstance(expires, bool)
        or not isinstance(expires, int | float)
        or not math.isfinite(expires)
    ):
        raise ValueError("expires_at must be a finite UTC timestamp")
    if expires <= now:
        raise ValueError("Command expired")
    if operation == "set":
        values = obj.get("values")
        if "references" in obj or not is_modbus_value_map(values):
            raise ValueError("set requires a values object")
        if any(isinstance(v, float) and not math.isfinite(v) for v in values.values()):
            raise ValueError("Values must be finite")
    elif operation == "get":
        names = obj.get("references")
        if "values" in obj or not isinstance(names, list):
            raise ValueError("get requires a references list")
        names = cast(list[object], names)
        if (
            not names
            or not all(isinstance(n, str) for n in names)
            or len(names) != len(set(cast(list[str], names)))
        ):
            raise ValueError("references must contain distinct strings")
        values = {cast(str, n): None for n in names}
    else:
        raise ValueError("operation must be get or set")
    if not values:
        raise ValueError("Empty command")
    return Command(
        cast(str, operation),
        cast(dict[str, ModbusValue], values),
        identifier,
        float(expires),
    )


class RequestCache:
    """Caches results only for this process; never promises exactly-once delivery."""

    def __init__(
        self, size: int = 1000, ttl: float = 300.0, max_bytes: int = 8388608
    ) -> None:
        self.size, self.ttl, self.max_bytes = size, ttl, max_bytes
        self.byte_count = 0
        self.entries: OrderedDict[tuple[str, str], tuple[float, str, str]] = (
            OrderedDict()
        )

    def lookup(self, device: str, identifier: str, payload: bytes) -> str | None:
        self._expire()
        entry = self.entries.get((device, identifier))
        if entry is None:
            return None
        if entry[1] != hashlib.sha256(payload).hexdigest():
            raise ValueError("request_id reused with a different payload")
        return entry[2]

    def save(self, device: str, identifier: str, payload: bytes, response: str) -> None:
        self._expire()
        key = (device, identifier)
        previous = self.entries.pop(key, None)
        if previous:
            self.byte_count -= len(previous[2].encode("utf-8"))
        self.byte_count += len(response.encode("utf-8"))
        self.entries[key] = (
            time.monotonic(),
            hashlib.sha256(payload).hexdigest(),
            response,
        )
        while len(self.entries) > self.size or self.byte_count > self.max_bytes:
            self._pop_oldest()

    def _expire(self) -> None:
        now = time.monotonic()
        while self.entries and next(iter(self.entries.values()))[0] + self.ttl < now:
            self._pop_oldest()

    def _pop_oldest(self) -> None:
        _, entry = self.entries.popitem(last=False)
        self.byte_count -= len(entry[2].encode("utf-8"))
