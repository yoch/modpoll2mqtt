"""Shared helpers for reference-driven Modbus read/write paths."""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from .modbus_task import Device, ModbusHandler, Poller, Reference


def call_with_device_id(method, *args, device_id: int, **kwargs):
    return method(*args, device_id=device_id, **kwargs)


def find_device(handler: "ModbusHandler", device_name: str) -> Optional["Device"]:
    for dev in handler.deviceList:
        if dev.name == device_name:
            return dev
    return None


def find_poller_for_ref(dev: "Device", ref: "Reference") -> Optional["Poller"]:
    for poller in dev.pollerList:
        if ref.check_sanity(poller.start_address, poller.size, poller.fc):
            return poller
    return None
