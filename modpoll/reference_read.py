"""MQTT/reference-driven Modbus reads (targeted per-ref, not full Poller.poll())."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .reference_common import (
    call_with_device_id,
    find_poller_for_ref,
)

if TYPE_CHECKING:
    from .modbus_task import Device, ModbusHandler, Poller, Reference

# Fallback for devices that reject partial reads: Poller.poll() on the parent block
# (not implemented — see ROADMAP B2 for connect/close overhead).
# Future: --get-min-interval rate-limit if RTU abuse becomes an issue.


def read_references(
    handler: ModbusHandler,
    dev: Device,
    ref_names: list[str],
) -> tuple[dict[str, object], int, int]:
    """Read refs on demand. Returns (values, unknown_skips, read_failures)."""
    values: dict[str, object] = {}
    unknown_skips = 0
    read_failures = 0
    resolved: list[tuple[str, Reference, Poller]] = []

    for ref_name in ref_names:
        ref = dev.references.get(ref_name)
        if ref is None:
            handler.logger.warning(
                f"Unknown reference '{ref_name}' on device {dev.name}, skipping"
            )
            unknown_skips += 1
            continue
        poller = find_poller_for_ref(dev, ref)
        if poller is None:
            handler.logger.error(
                f"No poller found for reference '{ref_name}' on device {dev.name}"
            )
            read_failures += 1
            continue
        if poller.disabled:
            handler.logger.warning(
                f"Poller disabled for reference '{ref_name}' on device {dev.name}"
            )
            read_failures += 1
            continue
        resolved.append((ref_name, ref, poller))

    coil_refs = [(n, r, p) for n, r, p in resolved if p.fc in (1, 2)]
    batches = _group_register_batches(resolved)

    for ref_name, ref, poller in coil_refs:
        try:
            value = _read_coil_reference(handler, dev, ref, poller)
            if value is None:
                read_failures += 1
                continue
            ref.update_value(value)
            values[ref_name] = ref.val
        except (ValueError, IndexError) as e:
            handler.logger.error(
                f"Modbus read error for reference '{ref_name}' on device {dev.name}: {e}"
            )
            read_failures += 1

    for batch in batches:
        batch_values, batch_failures = _read_register_batch(handler, dev, batch)
        read_failures += batch_failures
        if batch_values is None:
            continue
        for ref_name, value in batch_values.items():
            ref = batch.ref_by_name[ref_name]
            ref.update_value(value)
            values[ref_name] = ref.val

    return values, unknown_skips, read_failures


class _RegisterBatch:
    __slots__ = ("poller", "read_start", "read_count", "ref_by_name")

    def __init__(
        self,
        poller: Poller,
        read_start: int,
        read_count: int,
        ref_by_name: dict[str, Reference],
    ):
        self.poller = poller
        self.read_start = read_start
        self.read_count = read_count
        self.ref_by_name = ref_by_name


def _group_register_batches(
    resolved: list[tuple[str, Reference, Poller]],
) -> list[_RegisterBatch]:
    register_items = [
        (name, ref, poller) for name, ref, poller in resolved if poller.fc in (3, 4)
    ]
    if not register_items:
        return []

    batches: list[_RegisterBatch] = []
    by_poller: dict[int, list[tuple[str, Reference, Poller]]] = {}
    for item in register_items:
        by_poller.setdefault(id(item[2]), []).append(item)

    for items in by_poller.values():
        items.sort(key=lambda x: x[1].address)
        poller = items[0][2]
        batch_start = items[0][1].address
        batch_end = items[0][1].address + items[0][1].ref_width
        ref_by_name: dict[str, Reference] = {items[0][0]: items[0][1]}

        for name, ref, _ in items[1:]:
            ref_end = ref.address + ref.ref_width
            if ref.address <= batch_end:
                batch_end = max(batch_end, ref_end)
                ref_by_name[name] = ref
            else:
                batches.append(
                    _RegisterBatch(
                        poller,
                        batch_start,
                        batch_end - batch_start,
                        ref_by_name,
                    )
                )
                batch_start = ref.address
                batch_end = ref_end
                ref_by_name = {name: ref}

        batches.append(
            _RegisterBatch(poller, batch_start, batch_end - batch_start, ref_by_name)
        )

    return batches


def _coil_read_params(ref: Reference, poller: Poller) -> tuple[int, int]:
    if ref.dtype == "bool" and ref.bit is None:
        return ref.address, 1
    if ref.dtype in ("bool8", "bool16"):
        width = 8 if ref.dtype == "bool8" else 16
        group_offset = ref.address - poller.start_address
        coil_start = poller.start_address + group_offset * 8
        return coil_start, width
    raise ValueError(f"Unsupported coil dtype '{ref.dtype}' for reference '{ref.name}'")


def _read_coils(handler: ModbusHandler, dev: Device, address: int, count: int):
    result = call_with_device_id(
        handler.modbus_client.read_coils,
        address,
        count=count,
        device_id=dev.devid,
    )
    if result is not None and not result.isError():
        return result.bits
    return None


def _read_discrete_inputs(
    handler: ModbusHandler, dev: Device, address: int, count: int
):
    result = call_with_device_id(
        handler.modbus_client.read_discrete_inputs,
        address,
        count=count,
        device_id=dev.devid,
    )
    if result is not None and not result.isError():
        return result.bits
    return None


def _read_holding_registers(
    handler: ModbusHandler, dev: Device, address: int, count: int
):
    result = call_with_device_id(
        handler.modbus_client.read_holding_registers,
        address,
        count=count,
        device_id=dev.devid,
    )
    if result is not None and not result.isError():
        return result.registers
    return None


def _read_input_registers(
    handler: ModbusHandler, dev: Device, address: int, count: int
):
    result = call_with_device_id(
        handler.modbus_client.read_input_registers,
        address,
        count=count,
        device_id=dev.devid,
    )
    if result is not None and not result.isError():
        return result.registers
    return None


def _read_coil_reference(
    handler: ModbusHandler,
    dev: Device,
    ref: Reference,
    poller: Poller,
):
    address, count = _coil_read_params(ref, poller)
    if poller.fc == 1:
        bits = _read_coils(handler, dev, address, count)
    else:
        bits = _read_discrete_inputs(handler, dev, address, count)
    if bits is None:
        return None
    return poller.decode_coil_bits(ref, bits, address)


def _read_register_batch(
    handler: ModbusHandler,
    dev: Device,
    batch: _RegisterBatch,
) -> tuple[dict[str, object] | None, int]:
    poller = batch.poller
    if poller.fc == 3:
        registers = _read_holding_registers(
            handler, dev, batch.read_start, batch.read_count
        )
    else:
        registers = _read_input_registers(
            handler, dev, batch.read_start, batch.read_count
        )
    if registers is None:
        return None, len(batch.ref_by_name)

    out: dict[str, object] = {}
    failures = 0
    for ref_name, ref in batch.ref_by_name.items():
        try:
            out[ref_name] = poller.decode_register_block(
                ref, registers, batch.read_start
            )
        except (ValueError, IndexError) as e:
            handler.logger.error(
                f"Failed to decode reference '{ref_name}' on device {dev.name}: {e}"
            )
            failures += 1
    return out, failures
