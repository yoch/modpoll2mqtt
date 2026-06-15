import csv
import io
from unittest.mock import MagicMock

import pytest

from modpoll.modbus_task import Device, ModbusHandler, Poller, Reference
from modpoll.register_decode import Endian, RegisterDecoder, RegisterEncoder
from tests.helpers.modbus import FakeModbusMaster, handler_with_device


def test_encoder_round_trip_int16_and_float32():
    for endian in _ENDIAN_MAP_KEYS():
        byteorder, wordorder = _endian_pair(endian)
        original = [12345, 3.14159]
        encoder = RegisterEncoder(byteorder=byteorder, wordorder=wordorder)
        encoder.encode_16bit_int(original[0])
        encoder.encode_32bit_float(original[1])
        registers = encoder.to_registers()

        decoder = RegisterDecoder.from_registers(
            registers, byteorder=byteorder, wordorder=wordorder
        )
        assert decoder.decode_16bit_int() == original[0]
        assert decoder.decode_32bit_float() == pytest.approx(original[1])


def _ENDIAN_MAP_KEYS():
    return ["BE_BE", "LE_BE", "LE_LE", "BE_LE"]


def _endian_pair(endian):
    from modpoll.register_decode import ENDIAN_MAP

    return ENDIAN_MAP[endian]


def test_write_coil_bool8_read_modify_write():
    device = Device("dev", 1)
    poller = Poller(device, 1, 0, 16, "BE_BE")
    ref = Reference(device, "flags", "1", "bool8", "rw", None, None)
    poller.add_readable_reference(ref)
    device.pollerList = [poller]
    device.references = {"flags": ref}

    coils = [False] * 16
    master = FakeModbusMaster(coils=coils)
    handler = handler_with_device(device, master)
    new_flags = [True, False, True, False, False, False, False, False]
    assert handler.write_reference("dev", "flags", new_flags) is True
    assert master.writes[0][0] == "coils"
    assert master.writes[0][1] == 0
    written = master.writes[0][2]
    assert written[8:16] == new_flags


def test_write_holding_bool16_read_modify_write():
    device = Device("dev", 1)
    poller = Poller(device, 3, 0, 2, "BE_BE")
    ref = Reference(device, "flags", "0", "bool16", "rw", None, None)
    poller.add_readable_reference(ref)
    device.pollerList = [poller]
    device.references = {"flags": ref}

    master = FakeModbusMaster(registers=[0x0000])
    handler = handler_with_device(device, master)
    new_flags = [True] + [False] * 15
    assert handler.write_reference("dev", "flags", new_flags) is True
    assert master.writes == [("registers", 0, [0x0001])]


def test_write_coil_bool():
    device = Device("dev", 1)
    poller = Poller(device, 1, 0, 8, "BE_BE")
    ref = Reference(device, "cmd", "2", "bool", "rw", None, None)
    poller.add_readable_reference(ref)
    device.pollerList = [poller]
    device.references = {"cmd": ref}

    master = FakeModbusMaster(coils=[False] * 8)
    handler = handler_with_device(device, master)
    assert handler.write_reference("dev", "cmd", True) is True
    assert master.writes == [("coil", 2, True)]


def test_write_holding_int16_with_scale():
    device = Device("cta_conf", 1)
    poller = Poller(device, 3, 0, 4, "BE_BE")
    ref = Reference(device, "setpoint", "0", "int16", "rw", "°C", 0.1)
    poller.add_readable_reference(ref)
    device.pollerList = [poller]
    device.references = {"setpoint": ref}

    master = FakeModbusMaster(registers=[0, 0, 0, 0])
    handler = handler_with_device(device, master)
    assert handler.write_reference("cta_conf", "setpoint", 21.5) is True
    assert master.writes == [("register", 0, 215)]


def test_write_register_bit_read_modify_write():
    device = Device("dev", 1)
    poller = Poller(device, 3, 0, 1, "BE_BE")
    ref = Reference(device, "flag", "0:3", "bool", "rw", None, None)
    poller.add_readable_reference(ref)
    device.pollerList = [poller]
    device.references = {"flag": ref}

    master = FakeModbusMaster(registers=[0x0000])
    handler = handler_with_device(device, master)
    assert handler.write_reference("dev", "flag", True) is True
    assert master.writes == [("register", 0, 0x0008)]


def test_write_references_batch(caplog):
    device = Device("cta_conf", 1)
    poller = Poller(device, 3, 0, 2, "BE_BE")
    setpoint = Reference(device, "setpoint", "0", "int16", "rw", None, None)
    gain = Reference(device, "gain", "1", "int16", "rw", None, None)
    poller.add_readable_reference(setpoint)
    poller.add_readable_reference(gain)
    device.pollerList = [poller]
    device.references = {"setpoint": setpoint, "gain": gain}

    master = FakeModbusMaster(registers=[0, 0])
    handler = handler_with_device(device, master)
    with caplog.at_level("INFO"):
        handler.write_references("cta_conf", {"setpoint": 10, "gain": 20})

    assert master.writes == [("register", 0, 10), ("register", 1, 20)]
    assert "Wrote 2 value(s) for device=cta_conf" in caplog.text


def test_write_references_skips_unknown(caplog):
    device = Device("dev", 1)
    poller = Poller(device, 3, 0, 1, "BE_BE")
    ref = Reference(device, "setpoint", "0", "int16", "rw", None, None)
    poller.add_readable_reference(ref)
    device.pollerList = [poller]
    device.references = {"setpoint": ref}

    master = FakeModbusMaster(registers=[0])
    handler = handler_with_device(device, master)
    with caplog.at_level("WARNING"):
        handler.write_references("dev", {"setpoint": 10, "missing": 1})

    assert master.writes == [("register", 0, 10)]
    assert "Unknown reference 'missing'" in caplog.text


def test_write_references_empty_after_filter(caplog):
    device = Device("dev", 1)
    device.pollerList = []
    device.references = {}

    handler = handler_with_device(device, MagicMock())
    with caplog.at_level("WARNING"):
        handler.write_references("dev", {"missing": 1})

    assert "No known references in write payload" in caplog.text


def test_write_rejects_read_only_reference():
    device = Device("dev", 1)
    poller = Poller(device, 3, 0, 1, "BE_BE")
    ref = Reference(device, "temp", "0", "int16", "r", None, None)
    poller.add_readable_reference(ref)
    device.pollerList = [poller]
    device.references = {"temp": ref}

    master = FakeModbusMaster(registers=[100])
    handler = handler_with_device(device, master)
    assert handler.write_reference("dev", "temp", 10) is False
    assert master.writes == []


def test_write_rejects_input_register():
    device = Device("dev", 1)
    poller = Poller(device, 4, 0, 1, "BE_BE")
    ref = Reference(device, "input", "0", "uint16", "rw", None, None)
    device.pollerList = [poller]
    device.references = {"input": ref}

    master = FakeModbusMaster(registers=[0])
    handler = handler_with_device(device, master)
    assert handler.write_reference("dev", "input", 1) is False


def test_same_ref_name_on_two_devices():
    device_a = Device("cta_conf", 1)
    poller_a = Poller(device_a, 3, 0, 1, "BE_BE")
    ref_a = Reference(device_a, "setpoint", "0", "int16", "rw", None, None)
    poller_a.add_readable_reference(ref_a)
    device_a.pollerList = [poller_a]
    device_a.references = {"setpoint": ref_a}

    device_b = Device("cta_rest", 1)
    poller_b = Poller(device_b, 3, 7, 1, "BE_BE")
    ref_b = Reference(device_b, "setpoint", "7", "int16", "rw", None, None)
    poller_b.add_readable_reference(ref_b)
    device_b.pollerList = [poller_b]
    device_b.references = {"setpoint": ref_b}

    master = FakeModbusMaster(registers=[0] * 10)
    handler = ModbusHandler(master, "dummy.csv")
    handler.deviceList = [device_a, device_b]

    assert handler.write_reference("cta_conf", "setpoint", 10) is True
    assert handler.write_reference("cta_rest", "setpoint", 20) is True
    assert master.writes == [
        ("register", 0, 10),
        ("register", 7, 20),
    ]


def test_duplicate_reference_name_aborts_config(caplog):
    config = "\n".join(
        [
            "device,dev,1",
            "poll,holding_register,0,2,BE_BE",
            "ref,dup,0,uint16,r",
            "ref,dup,1,uint16,r",
        ]
    )
    handler = ModbusHandler(MagicMock(), "unused")
    with caplog.at_level("ERROR"):
        devices = handler._parse_config(csv.reader(io.StringIO(config)))
    assert devices == []
    assert "Duplicate reference name 'dup'" in caplog.text


def test_same_ref_allowed_on_different_devices_in_config(caplog):
    config = "\n".join(
        [
            "device,cta_conf,1",
            "poll,holding_register,0,1,BE_BE",
            "ref,setpoint,0,int16,rw",
            "device,cta_rest,1",
            "poll,holding_register,7,1,BE_BE",
            "ref,setpoint,7,int16,rw",
        ]
    )
    handler = ModbusHandler(MagicMock(), "unused")
    with caplog.at_level("ERROR"):
        devices = handler._parse_config(csv.reader(io.StringIO(config)))
    assert len(devices) == 2
    assert devices[0].references["setpoint"].address == 0
    assert devices[1].references["setpoint"].address == 7
