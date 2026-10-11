"""MQTT/reference-driven Modbus writes (inverse of poll decode path)."""

from __future__ import annotations

import logging
import math
import struct
from collections.abc import Sequence
from typing import Protocol, TypeGuard

from .modbus_models import Device, Poller, Reference
from .reference_common import call_with_device_id as _call_with_device_id
from .reference_common import find_device as _find_device
from .reference_common import find_poller_for_ref as _find_poller_for_ref
from .register_decode import ENDIAN_MAP, RegisterEncoder
from .types import ModbusClient, ModbusValue, is_bool_list, is_numeric_modbus_value


class _WriteHandler(Protocol):
    bit_write_strategy: str
    modbus_client: ModbusClient
    logger: logging.Logger

    @property
    def deviceList(self) -> Sequence[Device]: ...


def _value_to_raw(ref: Reference, value: ModbusValue) -> ModbusValue:
    if ref.scale is not None and is_numeric_modbus_value(value):
        raw = value / float(ref.scale)
        if ref.dtype in (
            "int16",
            "int32",
            "int64",
            "uint16",
            "uint32",
            "uint64",
        ):
            return int(round(raw))
        return raw
    return value


def _get_encoder(poller: Poller) -> RegisterEncoder:
    byteorder, wordorder = ENDIAN_MAP[poller.endian.strip().upper()]
    return RegisterEncoder(byteorder=byteorder, wordorder=wordorder)


def _expect_bool_list(
    value: ModbusValue, width: int, ref: Reference, logger: logging.Logger
) -> TypeGuard[list[bool]]:
    if not is_bool_list(value, width):
        logger.error(f"Reference '{ref.name}' expects a list of {width} booleans")
        return False
    return True


def validate_write(
    handler: _WriteHandler, device_name: str, ref_name: str, value: ModbusValue
) -> tuple[Device, Reference, Poller]:
    dev = _find_device(handler, device_name)
    if dev is None:
        raise ValueError(f"Device {device_name} not found")
    ref = dev.references.get(ref_name)
    if ref is None:
        raise ValueError(f"Unknown reference '{ref_name}'")
    poller = _find_poller_for_ref(dev, ref)
    if poller is None:
        raise ValueError(f"No unambiguous poller for '{ref_name}'")
    if "w" not in ref.rw or poller.fc not in (1, 3):
        raise ValueError(
            f"Reference '{ref_name}' is not writable (read-only Modbus object)"
        )
    if poller.disabled:
        raise ValueError(f"Poller disabled for '{ref_name}'")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("Value must be finite")
    if ref.dtype == "bool":
        if not isinstance(value, bool):
            raise ValueError("Expected a boolean")
    elif ref.dtype in ("bool8", "bool16"):
        width = 8 if ref.dtype == "bool8" else 16
        if not is_bool_list(value, width):
            raise ValueError(f"Expected {width} booleans")
        if (
            poller.fc == 1
            and (ref.address - poller.start_address) * 8 + width > poller.size
        ):
            raise ValueError("Cannot write a partial coil group")
    elif ref.dtype.startswith("string"):
        if not isinstance(value, str) or len(value.encode("utf-8")) > ref.string_bytes:
            raise ValueError(f"Expected at most {ref.string_bytes} UTF-8 bytes")
    else:
        raw = _value_to_raw(ref, value)
        if not is_numeric_modbus_value(raw):
            raise ValueError("Expected a numeric value")
        if isinstance(raw, float) and not math.isfinite(raw):
            raise ValueError("Scaled value must be finite")
        if ref.dtype.startswith(("int", "uint")):
            bits = int(ref.dtype.lstrip("uint"))
            signed = ref.dtype.startswith("int")
            low, high = (
                (-(1 << (bits - 1)), (1 << (bits - 1)) - 1)
                if signed
                else (0, (1 << bits) - 1)
            )
            if isinstance(raw, float) and not raw.is_integer():
                raise ValueError("Integer reference requires an integral value")
            if not low <= raw <= high:
                raise ValueError(f"Value outside {ref.dtype} range")
        else:
            bits = int(ref.dtype[5:])
            try:
                struct.pack({16: "e", 32: "f", 64: "d"}[bits], float(raw))
            except (OverflowError, struct.error) as exc:
                raise ValueError(f"Value outside {ref.dtype} range") from exc
    return dev, ref, poller


def write_reference(
    handler: _WriteHandler, device_name: str, ref_name: str, value: ModbusValue
) -> bool:
    try:
        dev, ref, poller = validate_write(handler, device_name, ref_name, value)
        if poller.fc == 1:
            return _write_coil_reference(handler, dev, ref, poller, value)
        return _write_register_reference(handler, dev, ref, poller, value)
    except (ValueError, IndexError, OverflowError, struct.error) as exc:
        handler.logger.error("Invalid write %s/%s: %s", device_name, ref_name, exc)
        return False


def _write_coil(handler: _WriteHandler, dev: Device, address: int, value: bool) -> bool:
    result = _call_with_device_id(
        handler.modbus_client.write_coil,
        address,
        value,
        device_id=dev.devid,
    )
    return not result.isError()


def _write_coils(
    handler: _WriteHandler, dev: Device, address: int, values: list[bool]
) -> bool:
    result = _call_with_device_id(
        handler.modbus_client.write_coils,
        address,
        values,
        device_id=dev.devid,
    )
    return not result.isError()


def _write_register(
    handler: _WriteHandler, dev: Device, address: int, value: int
) -> bool:
    result = _call_with_device_id(
        handler.modbus_client.write_register,
        address,
        value,
        device_id=dev.devid,
    )
    return not result.isError()


def _write_registers(
    handler: _WriteHandler, dev: Device, address: int, values: list[int]
) -> bool:
    result = _call_with_device_id(
        handler.modbus_client.write_registers,
        address,
        values,
        device_id=dev.devid,
    )
    return not result.isError()


def _read_holding_registers(
    handler: _WriteHandler, dev: Device, address: int, count: int
) -> list[int] | None:
    result = _call_with_device_id(
        handler.modbus_client.read_holding_registers,
        address,
        count=count,
        device_id=dev.devid,
    )
    if not result.isError():
        return result.registers
    return None


def _write_coil_reference(
    handler: _WriteHandler,
    dev: Device,
    ref: Reference,
    poller: Poller,
    value: ModbusValue,
) -> bool:
    if ref.dtype == "bool" and ref.bit is None:
        if not isinstance(value, bool):
            handler.logger.error(f"Reference '{ref.name}' expects a boolean value")
            return False
        return _write_coil(handler, dev, ref.address, value)

    if ref.dtype in ("bool8", "bool16"):
        width = 8 if ref.dtype == "bool8" else 16
        if not _expect_bool_list(value, width, ref, handler.logger):
            return False
        address = poller.start_address + (ref.address - poller.start_address) * 8
        return _write_coils(handler, dev, address, value)

    handler.logger.error(
        f"Unsupported dtype '{ref.dtype}' for coil write on reference '{ref.name}'"
    )
    return False


def _mask_write(
    handler: _WriteHandler,
    dev: Device,
    ref: Reference,
    poller: Poller,
    mask: int,
    value: int,
) -> bool:
    encoder = _get_encoder(poller)
    encoder.encode_16bit_uint(mask)
    raw_mask = encoder.to_registers()[0]
    encoder = _get_encoder(poller)
    encoder.encode_16bit_uint(value)
    raw_value = encoder.to_registers()[0]
    result = _call_with_device_id(
        handler.modbus_client.mask_write_register,
        address=ref.address,
        and_mask=(~raw_mask) & 0xFFFF,
        or_mask=raw_value & raw_mask,
        device_id=dev.devid,
    )
    return not result.isError()


def _write_register_reference(
    handler: _WriteHandler,
    dev: Device,
    ref: Reference,
    poller: Poller,
    value: ModbusValue,
) -> bool:
    if ref.dtype == "bool" and ref.bit is not None:
        if not isinstance(value, bool):
            handler.logger.error(f"Reference '{ref.name}' expects a boolean value")
            return False
        if handler.bit_write_strategy == "mask":
            return _mask_write(
                handler, dev, ref, poller, 1 << ref.bit, (1 << ref.bit) if value else 0
            )
        registers = _read_holding_registers(handler, dev, ref.address, 1)
        if registers is None:
            return False
        if len(registers) != 1:
            return False
        word = poller.get_decoder(registers).decode_16bit_uint()
        word = word | (1 << ref.bit) if value else word & ~(1 << ref.bit)
        encoder = _get_encoder(poller)
        encoder.encode_16bit_uint(word)
        raw = encoder.to_registers()[0]
        return _write_register(handler, dev, ref.address, raw)

    if ref.dtype in ("bool8", "bool16"):
        width = 8 if ref.dtype == "bool8" else 16
        if not _expect_bool_list(value, width, ref, handler.logger):
            return False
        if handler.bit_write_strategy == "mask":
            word = sum(int(bit) << i for i, bit in enumerate(value))
            return _mask_write(handler, dev, ref, poller, (1 << width) - 1, word)
        registers = _read_holding_registers(handler, dev, ref.address, 1)
        if registers is None or len(registers) != 1:
            return False
        word = poller.get_decoder(registers).decode_16bit_uint()
        for i, bit_val in enumerate(value):
            if bit_val:
                word |= 1 << i
            else:
                word &= ~(1 << i)
        encoder = _get_encoder(poller)
        encoder.encode_16bit_uint(word)
        return _write_registers(handler, dev, ref.address, encoder.to_registers())

    raw_value = _value_to_raw(ref, value)
    encoder = _get_encoder(poller)
    float_encoders = {
        "float16": encoder.encode_16bit_float,
        "float32": encoder.encode_32bit_float,
        "float64": encoder.encode_64bit_float,
    }
    int_encoders = {
        "uint16": encoder.encode_16bit_uint,
        "int16": encoder.encode_16bit_int,
        "uint32": encoder.encode_32bit_uint,
        "int32": encoder.encode_32bit_int,
        "uint64": encoder.encode_64bit_uint,
        "int64": encoder.encode_64bit_int,
    }

    if ref.dtype in float_encoders:
        if not is_numeric_modbus_value(raw_value):
            handler.logger.error(f"Reference '{ref.name}' expects a numeric value")
            return False
        float_encoders[ref.dtype](float(raw_value))
        registers = encoder.to_registers()
        if len(registers) == 1:
            return _write_register(handler, dev, ref.address, registers[0])
        return _write_registers(handler, dev, ref.address, registers)

    if ref.dtype in int_encoders:
        if not is_numeric_modbus_value(raw_value):
            handler.logger.error(f"Reference '{ref.name}' expects a numeric value")
            return False
        int_encoders[ref.dtype](int(round(raw_value)))
        registers = encoder.to_registers()
        if len(registers) == 1:
            return _write_register(handler, dev, ref.address, registers[0])
        return _write_registers(handler, dev, ref.address, registers)

    if ref.dtype.startswith("string"):
        if not isinstance(value, str):
            handler.logger.error(f"Reference '{ref.name}' expects a string value")
            return False
        size = ref.string_bytes
        # Apply word order to complete registers, then fill the reserved pad byte.
        size = ref.ref_width * 2
        encoder.encode_string(value, size)
        registers = encoder.to_registers()
        return _write_registers(handler, dev, ref.address, registers)

    handler.logger.error(
        f"Unsupported dtype '{ref.dtype}' for register write on reference '{ref.name}'"
    )
    return False
