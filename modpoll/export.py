"""Aggregate fresh values and atomically replace a JSON snapshot."""

from __future__ import annotations

import json
import math
import os
import tempfile
from collections.abc import Sequence
from pathlib import Path

from .modbus_models import Device
from .types import ModbusValue


def export_snapshot(
    file: str, devices: Sequence[Device], timestamp: float | None = None
) -> None:
    data: dict[str, dict[str, ModbusValue]] = {}
    for device in devices:
        values = {
            ref.name: ref.val
            for ref in device.references.values()
            if not (isinstance(ref.val, float) and not math.isfinite(ref.val))
        }
        if timestamp is not None:
            values["timestamp"] = timestamp
        data[device.name] = values
    destination = Path(file)
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            delete=False,
        ) as stream:
            temporary = stream.name
            json.dump(data, stream, indent=2, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
        temporary = None
    finally:
        if temporary is not None:
            os.unlink(temporary)
