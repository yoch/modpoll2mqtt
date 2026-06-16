from unittest.mock import MagicMock

import pytest
from tests.helpers.modbus import FakeModbusMaster, handler_with_device

from modpoll.modbus_models import Device, Poller, Reference


def test_read_holding_uint16():
    device = Device("dev", 1)
    poller = Poller(device, 3, 0, 44, "BE_BE")
    ref = Reference(device, "holding_reg01", "0", "uint16", "rw", None, None)
    poller.add_readable_reference(ref)
    device.pollerList = [poller]
    device.references = {"holding_reg01": ref}

    master = FakeModbusMaster(registers=[0x1234] + [0] * 43)
    handler = handler_with_device(device, master)
    result = handler.read_references("dev", ["holding_reg01"])

    assert result == {"holding_reg01": 0x1234}
    assert ref.val == 0x1234
    assert device.getCount == 1
    assert device.getErrors == 0
    assert device.getSuccess == 1


def test_read_targeted_single_register_not_full_poller():
    device = Device("dev", 1)
    poller = Poller(device, 3, 40000, 44, "BE_BE")
    ref = Reference(device, "holding_reg01", "40000", "uint16", "rw", None, None)
    poller.add_readable_reference(ref)
    device.pollerList = [poller]
    device.references = {"holding_reg01": ref}

    master = FakeModbusMaster(registers=[0] * 40050)
    master.registers[40000] = 99
    handler = handler_with_device(device, master)

    handler.read_references("dev", ["holding_reg01"])

    assert ref.val == 99


def test_read_batch_adjacent_registers_single_modbus_read():
    device = Device("dev", 1)
    poller = Poller(device, 3, 0, 4, "BE_BE")
    a = Reference(device, "a", "0", "uint16", "rw", None, None)
    b = Reference(device, "b", "1", "uint16", "rw", None, None)
    poller.add_readable_reference(a)
    poller.add_readable_reference(b)
    device.pollerList = [poller]
    device.references = {"a": a, "b": b}

    master = FakeModbusMaster(registers=[10, 20])
    handler = handler_with_device(device, master)
    result = handler.read_references("dev", ["a", "b"])

    assert result == {"a": 10, "b": 20}


def test_read_coil_bool():
    device = Device("dev", 1)
    poller = Poller(device, 1, 0, 8, "BE_BE")
    ref = Reference(device, "cmd", "2", "bool", "rw", None, None)
    poller.add_readable_reference(ref)
    device.pollerList = [poller]
    device.references = {"cmd": ref}

    coils = [False] * 8
    coils[2] = True
    master = FakeModbusMaster(coils=coils)
    handler = handler_with_device(device, master)

    result = handler.read_references("dev", ["cmd"])

    assert result == {"cmd": True}


def test_read_int16_with_scale():
    device = Device("dev", 1)
    poller = Poller(device, 3, 0, 1, "BE_BE")
    ref = Reference(device, "temp", "0", "int16", "r", "°C", 0.1)
    poller.add_readable_reference(ref)
    device.pollerList = [poller]
    device.references = {"temp": ref}

    master = FakeModbusMaster(registers=[215])
    handler = handler_with_device(device, master)
    result = handler.read_references("dev", ["temp"])

    assert result == {"temp": pytest.approx(21.5)}


def test_read_partial_unknown_reference():
    device = Device("dev", 1)
    poller = Poller(device, 3, 0, 1, "BE_BE")
    ref = Reference(device, "a", "0", "uint16", "r", None, None)
    poller.add_readable_reference(ref)
    device.pollerList = [poller]
    device.references = {"a": ref}

    master = FakeModbusMaster(registers=[1])
    handler = handler_with_device(device, master)
    result = handler.read_references("dev", ["a", "missing"])

    assert result == {"a": 1}
    assert device.getCount == 1
    assert device.getErrors == 1
    assert device.getSuccess == 0
    assert device.getUnknownRefs == 1
    assert device.getReadErrors == 0


def test_read_disabled_poller_returns_empty():
    device = Device("dev", 1)
    poller = Poller(device, 3, 0, 1, "BE_BE")
    poller.disabled = True
    ref = Reference(device, "a", "0", "uint16", "r", None, None)
    poller.add_readable_reference(ref)
    device.pollerList = [poller]
    device.references = {"a": ref}

    handler = handler_with_device(device, FakeModbusMaster(registers=[1]))
    assert handler.read_references("dev", ["a"]) == {}
    assert device.getCount == 1
    assert device.getErrors == 1
    assert device.getReadErrors == 1


def test_read_modbus_error_returns_empty():
    device = Device("dev", 1)
    poller = Poller(device, 3, 0, 1, "BE_BE")
    ref = Reference(device, "a", "0", "uint16", "r", None, None)
    poller.add_readable_reference(ref)
    device.pollerList = [poller]
    device.references = {"a": ref}

    master = FakeModbusMaster(registers=[0], fail_addresses={0})
    handler = handler_with_device(device, master)
    assert handler.read_references("dev", ["a"]) == {}
    assert device.getCount == 1
    assert device.getErrors == 1
    assert device.getReadErrors == 1


def test_read_empty_payload_does_not_count_attempt(caplog):
    device = Device("dev", 1)
    device.pollerList = []
    device.references = {}

    handler = handler_with_device(device, MagicMock())
    with caplog.at_level("WARNING"):
        assert handler.read_references("dev", []) == {}

    assert device.getErrors == 0
    assert device.getCount == 0
    assert "Empty MQTT get payload" in caplog.text
