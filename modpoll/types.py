"""Shared typing helpers for modpoll (incremental static typing)."""

from __future__ import annotations

from typing import TypeAlias, TypedDict, TypeGuard, cast

from pymodbus.client import ModbusSerialClient, ModbusTcpClient, ModbusUdpClient

# Concrete pymodbus clients used throughout the gateway (single contract).
ModbusClient: TypeAlias = ModbusSerialClient | ModbusTcpClient | ModbusUdpClient

# MQTT / reference scalar or vector values exposed in JSON payloads.
ModbusValue: TypeAlias = bool | int | float | str | list[bool] | None
NumericModbusValue: TypeAlias = int | float

# Reference map in a single MQTT set/get message.
MqttPayload: TypeAlias = dict[str, ModbusValue]


def is_modbus_value_map(obj: object) -> TypeGuard[dict[str, ModbusValue]]:
    """True when ``obj`` is a reference-name map from a Modbus read."""
    if not isinstance(obj, dict):
        return False
    candidate = cast(dict[object, object], obj)
    return all(
        isinstance(key, str) and is_modbus_value(value)
        for key, value in candidate.items()
    )


def is_modbus_value(obj: object) -> TypeGuard[ModbusValue]:
    """True when ``obj`` can be handled as a Modbus/MQTT reference value."""
    return obj is None or isinstance(obj, bool | int | float | str) or is_bool_list(obj)


def is_bool_list(obj: object, width: int | None = None) -> TypeGuard[list[bool]]:
    """True when ``obj`` is a boolean list, optionally with an exact width."""
    if not isinstance(obj, list):
        return False
    items = cast(list[object], obj)
    if width is not None and len(items) != width:
        return False
    return all(isinstance(item, bool) for item in items)


def is_numeric_modbus_value(obj: ModbusValue) -> TypeGuard[NumericModbusValue]:
    """True for numeric reference values, excluding bool despite bool subclassing int."""
    return not isinstance(obj, bool) and isinstance(obj, int | float)


class ModbusConnectionDiagnostics(TypedDict):
    modbus_connection_state: str
    modbus_connected: bool
    modbus_connected_since: float | None
    modbus_last_success_at: float | None
    modbus_last_failure_at: float | None
    modbus_last_error: str | None
    modbus_consecutive_failures: int
    modbus_backoff_until: float | None
    modbus_connect_count: int
    modbus_reconnect_count: int
    modbus_transaction_failure_count: int
