"""Shared helpers for reference-driven Modbus read/write paths."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Protocol, TypeVar

_Result = TypeVar("_Result")
_DeviceT = TypeVar("_DeviceT", bound="_NamedDevice", covariant=True)
_PollerT = TypeVar("_PollerT", bound="_PollerRange", covariant=True)


class _NamedDevice(Protocol):
    @property
    def name(self) -> str: ...


class _HasDeviceList(Protocol[_DeviceT]):
    @property
    def deviceList(self) -> Sequence[_DeviceT]: ...


class _PollerRange(Protocol):
    start_address: int
    size: int
    fc: int


class _HasPollerList(Protocol[_PollerT]):
    @property
    def pollerList(self) -> Sequence[_PollerT]: ...


class _ReferenceWithSanity(Protocol):
    def check_sanity(
        self, reference: int, size: int, fc: int | None = None
    ) -> bool: ...


def call_with_device_id(
    method: Callable[..., _Result],
    *args: object,
    device_id: int,
    **kwargs: object,
) -> _Result:
    """Call a pymodbus method with the project-wide device_id keyword.

    Pymodbus synchronous client calls return a response PDU, including error
    responses, or raise transport exceptions; None is not part of that contract.
    """
    return method(*args, device_id=device_id, **kwargs)


def find_device(handler: _HasDeviceList[_DeviceT], device_name: str) -> _DeviceT | None:
    for dev in handler.deviceList:
        if dev.name == device_name:
            return dev
    return None


def find_poller_for_ref(
    dev: _HasPollerList[_PollerT], ref: _ReferenceWithSanity
) -> _PollerT | None:
    for poller in dev.pollerList:
        if ref.check_sanity(poller.start_address, poller.size, poller.fc):
            return poller
    return None
