"""MQTT/reference-driven Modbus writes (inverse of poll decode path)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from pymodbus.exceptions import ModbusException

from .reference_common import (
    call_with_device_id as _call_with_device_id,
    find_device as _find_device,
    find_poller_for_ref as _find_poller_for_ref,
)
from .register_decode import ENDIAN_MAP, RegisterEncoder

if TYPE_CHECKING:
    from .modbus_task import Device, ModbusHandler, Poller, Reference


def _value_to_raw(ref: "Reference", value):
    if ref.scale and not isinstance(value, bool) and not isinstance(value, list):
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


def _get_encoder(poller: "Poller") -> RegisterEncoder:
    byteorder, wordorder = ENDIAN_MAP[poller.endian.strip().upper()]
    return RegisterEncoder(byteorder=byteorder, wordorder=wordorder)


def _expect_bool_list(ref: "Reference", value, width: int, logger) -> bool:
    if not isinstance(value, list) or len(value) != width:
        logger.error(f"Reference '{ref.name}' expects a list of {width} booleans")
        return False
    return True


def write_reference(
    handler: "ModbusHandler", device_name: str, ref_name: str, value
) -> bool:
    dev = _find_device(handler, device_name)
    if dev is None:
        handler.logger.error(f"Device {device_name} not found")
        return False

    ref = dev.references.get(ref_name)
    if ref is None:
        handler.logger.error(
            f"Reference '{ref_name}' not found on device {device_name}"
        )
        return False

    if "w" not in ref.rw:
        handler.logger.error(
            f"Reference '{ref_name}' on device {device_name} is not writable"
        )
        return False

    poller = _find_poller_for_ref(dev, ref)
    if poller is None:
        handler.logger.error(
            f"No poller found for reference '{ref_name}' on device {device_name}"
        )
        return False

    if poller.fc in (2, 4):
        handler.logger.error(
            f"Reference '{ref_name}' is on a read-only Modbus object type"
        )
        return False

    if poller.fc == 1:
        return _write_coil_reference(handler, dev, ref, poller, value)
    return _write_register_reference(handler, dev, ref, poller, value)


def _write_coil(handler: "ModbusHandler", dev: "Device", address: int, value) -> bool:
    try:
        result = _call_with_device_id(
            handler.modbus_client.write_coil,
            address,
            value,
            device_id=dev.devid,
        )
        return result is not None and not result.isError()
    except ModbusException as e:
        handler.logger.error(f"Error writing coil: {e}")
        return False


def _write_coils(handler: "ModbusHandler", dev: "Device", address: int, values) -> bool:
    try:
        result = _call_with_device_id(
            handler.modbus_client.write_coils,
            address,
            values,
            device_id=dev.devid,
        )
        return result is not None and not result.isError()
    except ModbusException as e:
        handler.logger.error(f"Error writing coils: {e}")
        return False


def _write_register(
    handler: "ModbusHandler", dev: "Device", address: int, value: int
) -> bool:
    try:
        result = _call_with_device_id(
            handler.modbus_client.write_register,
            address,
            value,
            device_id=dev.devid,
        )
        return result is not None and not result.isError()
    except ModbusException as e:
        handler.logger.error(f"Error writing register: {e}")
        return False


def _write_registers(
    handler: "ModbusHandler", dev: "Device", address: int, values
) -> bool:
    try:
        result = _call_with_device_id(
            handler.modbus_client.write_registers,
            address,
            values,
            device_id=dev.devid,
        )
        return result is not None and not result.isError()
    except ModbusException as e:
        handler.logger.error(f"Error writing registers: {e}")
        return False


def _read_coils(handler: "ModbusHandler", dev: "Device", address: int, count: int):
    try:
        result = _call_with_device_id(
            handler.modbus_client.read_coils,
            address,
            count=count,
            device_id=dev.devid,
        )
        if result is not None and not result.isError():
            return result.bits
    except ModbusException as e:
        handler.logger.error(f"Error reading coils: {e}")
    return None


def _read_holding_registers(
    handler: "ModbusHandler", dev: "Device", address: int, count: int
):
    try:
        result = _call_with_device_id(
            handler.modbus_client.read_holding_registers,
            address,
            count=count,
            device_id=dev.devid,
        )
        if result is not None and not result.isError():
            return result.registers
    except ModbusException as e:
        handler.logger.error(f"Error reading holding registers: {e}")
    return None


def _write_coil_reference(
    handler: "ModbusHandler",
    dev: "Device",
    ref: "Reference",
    poller: "Poller",
    value,
) -> bool:
    if ref.dtype == "bool" and ref.bit is None:
        if not isinstance(value, bool):
            handler.logger.error(f"Reference '{ref.name}' expects a boolean value")
            return False
        return _write_coil(handler, dev, ref.address, value)

    if ref.dtype in ("bool8", "bool16"):
        width = 8 if ref.dtype == "bool8" else 16
        if not _expect_bool_list(ref, value, width, handler.logger):
            return False
        group_offset = ref.address - poller.start_address
        bit_offset = group_offset * 8
        bits = _read_coils(handler, dev, poller.start_address, poller.size)
        if bits is None:
            return False
        current = list(bits)
        for i, bit_val in enumerate(value):
            current[bit_offset + i] = bool(bit_val)
        return _write_coils(handler, dev, poller.start_address, current)

    handler.logger.error(
        f"Unsupported dtype '{ref.dtype}' for coil write on reference '{ref.name}'"
    )
    return False


def _write_register_reference(
    handler: "ModbusHandler",
    dev: "Device",
    ref: "Reference",
    poller: "Poller",
    value,
) -> bool:
    if ref.dtype == "bool" and ref.bit is not None:
        if not isinstance(value, bool):
            handler.logger.error(f"Reference '{ref.name}' expects a boolean value")
            return False
        registers = _read_holding_registers(handler, dev, ref.address, 1)
        if registers is None:
            return False
        word = registers[0]
        if value:
            word |= 1 << ref.bit
        else:
            word &= ~(1 << ref.bit)
        return _write_register(handler, dev, ref.address, word & 0xFFFF)

    if ref.dtype in ("bool8", "bool16"):
        width = 8 if ref.dtype == "bool8" else 16
        if not _expect_bool_list(ref, value, width, handler.logger):
            return False
        registers = _read_holding_registers(handler, dev, ref.address, 1)
        if registers is None:
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
    encode_methods = {
        "uint16": encoder.encode_16bit_uint,
        "int16": encoder.encode_16bit_int,
        "uint32": encoder.encode_32bit_uint,
        "int32": encoder.encode_32bit_int,
        "uint64": encoder.encode_64bit_uint,
        "int64": encoder.encode_64bit_int,
        "float16": encoder.encode_16bit_float,
        "float32": encoder.encode_32bit_float,
        "float64": encoder.encode_64bit_float,
    }

    if ref.dtype in encode_methods:
        encode_methods[ref.dtype](raw_value)
        registers = encoder.to_registers()
        if len(registers) == 1:
            return _write_register(handler, dev, ref.address, registers[0])
        return _write_registers(handler, dev, ref.address, registers)

    if ref.dtype.startswith("string"):
        if not isinstance(value, str):
            handler.logger.error(f"Reference '{ref.name}' expects a string value")
            return False
        size = ref.ref_width * 2
        encoder.encode_string(value, size)
        registers = encoder.to_registers()
        return _write_registers(handler, dev, ref.address, registers)

    handler.logger.error(
        f"Unsupported dtype '{ref.dtype}' for register write on reference '{ref.name}'"
    )
    return False
