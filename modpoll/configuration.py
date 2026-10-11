"""Load and validate an entire configuration before opening any transport."""

from __future__ import annotations

import csv
import io
import logging
import math
from collections.abc import Iterator
from pathlib import Path
from typing import cast
from urllib.parse import urlsplit

import requests

from .modbus_models import Device, Poller, Reference
from .register_decode import ENDIAN_MAP

MAX_CONFIG_BYTES = 2 * 1024 * 1024
FUNCTION_CODES = {
    "coil": 1,
    "discrete_input": 2,
    "holding_register": 3,
    "input_register": 4,
}


class ConfigurationError(ValueError):
    """A source/line-specific configuration error."""


def safe_source(source: str) -> str:
    try:
        parsed = urlsplit(source)
    except ValueError:
        return "<invalid source>"
    return (
        f"{parsed.scheme}://{parsed.hostname}{parsed.path}"
        if parsed.scheme in ("http", "https")
        else str(Path(source))
    )


def load_text(source: str, timeout: float) -> str:
    try:
        if urlsplit(source).scheme in ("http", "https"):
            with requests.get(
                source, timeout=(timeout, timeout), stream=True
            ) as response:
                response.raise_for_status()
                content = bytearray()
                for chunk in response.iter_content(65536):
                    content.extend(chunk)
                    if len(content) > MAX_CONFIG_BYTES:
                        raise ConfigurationError("Configuration exceeds 2 MiB")
        else:
            with open(source, "rb") as stream:
                content = stream.read(MAX_CONFIG_BYTES + 1)
        if len(content) > MAX_CONFIG_BYTES:
            raise ConfigurationError("Configuration exceeds 2 MiB")
        return content.decode("utf-8-sig")
    except ConfigurationError:
        raise
    except (OSError, UnicodeError, ValueError, requests.RequestException) as exc:
        raise ConfigurationError(
            f"Cannot load {safe_source(source)} ({type(exc).__name__})"
        ) from exc


def validate_name(name: str) -> None:
    if not name or any(c in name for c in "/+#\x00") or any(ord(c) < 32 for c in name):
        raise ValueError(f"Invalid MQTT name: {name!r}")


def parse_config(
    rows: Iterator[list[str]], source: str, logger: logging.Logger
) -> list[Device]:
    devices: list[Device] = []
    device: Device | None = None
    poller: Poller | None = None
    seen: set[str] = set()
    line = 0
    try:
        for line, raw in enumerate(rows, 1):
            if (
                not raw
                or not any(cell.strip() for cell in raw)
                or raw[0].lstrip().startswith("#")
            ):
                continue
            row = [cell.strip() for cell in raw]
            kind = row[0].lower()
            if kind == "device":
                poller = None
                if len(row) < 3:
                    raise ValueError("Invalid device configuration")
                validate_name(row[1])
                if row[1] in seen:
                    raise ValueError(f"Duplicate device name '{row[1]}'")
                identifier = int(row[2], 0)
                if not 1 <= identifier <= 255:
                    raise ValueError(
                        "Device ID must be 1..255 (broadcast is unsupported)"
                    )
                device = Device(row[1], identifier)
                devices.append(device)
                seen.add(row[1])
            elif kind == "poll":
                poller = None
                if device is None or len(row) < 5:
                    raise ValueError("Poll requires a device and five columns")
                fc = FUNCTION_CODES.get(row[1].lower())
                if fc is None:
                    raise ValueError(f"Unknown function code: {row[1]}")
                start, size = int(row[2], 0), int(row[3], 0)
                maximum = 2000 if fc in (1, 2) else 125
                if (
                    not 0 <= start <= 65535
                    or not 1 <= size <= maximum
                    or start + size > 65536
                ):
                    raise ValueError(f"Invalid poll range (maximum count {maximum})")
                endian = row[4].upper()
                if endian not in ENDIAN_MAP:
                    raise ValueError(f"Invalid endian: {endian}")
                existing: Poller | None = next(
                    (
                        p
                        for p in device.pollerList
                        if (p.fc, p.start_address, p.size, p.endian.upper())
                        == (fc, start, size, endian)
                    ),
                    None,
                )
                if existing:
                    logger.warning(
                        "Duplicate poller on device %s; reusing it", device.name
                    )
                    poller = existing
                else:
                    poller = Poller(device, fc, start, size, endian)
                    device.pollerList.append(poller)
            elif kind == "ref":
                if device is None or poller is None or len(row) < 5:
                    raise ValueError(
                        "Reference requires a valid device/poller and five columns"
                    )
                poller = cast(Poller, poller)
                name = row[1].replace(" ", "_")
                validate_name(name)
                if name in device.references:
                    raise ValueError(f"Duplicate reference name '{name}'")
                permissions = row[4].lower() or "r"
                if permissions not in ("r", "w", "rw"):
                    raise ValueError(f"Invalid permissions: {permissions}")
                scale = float(row[6]) if len(row) > 6 and row[6] else None
                if scale is not None and (not math.isfinite(scale) or scale == 0):
                    raise ValueError("Scale must be finite and nonzero")
                ref = Reference(
                    device,
                    name,
                    row[2],
                    row[3],
                    permissions,
                    row[5] if len(row) > 5 else None,
                    scale,
                )
                if not ref.check_sanity(poller.start_address, poller.size, poller.fc):
                    raise ValueError(
                        f"Reference {name} is outside its poller or has an incompatible dtype"
                    )
                if poller.fc in (3, 4) and ref.dtype == "bool" and ref.bit is None:
                    raise ValueError("Register bool requires address:bit")
                if poller.fc in (2, 4) and "w" in permissions:
                    logger.warning(
                        "Reference %s marked writable on read-only %s; writes rejected",
                        name,
                        "discrete_input" if poller.fc == 2 else "input_register",
                    )
                ref.poller = poller
                device.add_reference_mapping(ref)
                if "r" in permissions:
                    poller.add_readable_reference(ref)
            else:
                raise ValueError(
                    f"Unknown row type: {row[0]}; check --csv-delimiter (comma, tab)"
                )
        if not devices or any(not d.pollerList or not d.references for d in devices):
            raise ValueError("Each device requires at least one poller and reference")
        return devices
    except (ValueError, IndexError, csv.Error) as exc:
        raise ConfigurationError(f"{safe_source(source)}:{line}: {exc}") from exc


def read_config(
    source: str, delimiter: str, timeout: float, logger: logging.Logger
) -> list[Device]:
    return parse_config(
        csv.reader(
            io.StringIO(load_text(source, timeout)), delimiter=delimiter, strict=True
        ),
        source,
        logger,
    )
