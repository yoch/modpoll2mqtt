import csv
from unittest.mock import MagicMock

from modpoll.modbus_models import Device, Poller, Reference
from modpoll.modbus_task import ModbusHandler


class FakeResult:
    def __init__(self, *, bits=None, registers=None):
        self.bits = bits
        self.registers = registers

    def isError(self):
        return False


def test_poller_uses_discrete_inputs_for_fc2():
    device = Device("dev", 1)
    poller = Poller(device, 2, 0, 2, "BE_BE")
    ref = Reference(device, "di0", "0", "bool", "r", None, None)
    poller.add_readable_reference(ref)

    class FakeMaster:
        def __init__(self):
            self.called = None

        def read_discrete_inputs(self, address, *, count=1, device_id=1):
            self.called = ("di", address, count)
            return FakeResult(bits=[1, 0])

        def read_coils(self, *args, **kwargs):  # pragma: no cover - should not be used
            raise AssertionError("read_coils should not be called for fc=2")

    master = FakeMaster()

    assert poller.poll(master) is True
    assert master.called == ("di", 0, 2)
    assert ref.val is True


def test_bit_reference_decodes_multiple_bits_same_register():
    device = Device("dev", 1)
    poller = Poller(device, 4, 40000, 1, "BE_BE")
    ref_msb = Reference(device, "bit15", "40000:15", "bool", "r", None, None)
    ref_lsb = Reference(device, "bit0", "40000:0", "bool", "r", None, None)
    poller.add_readable_reference(ref_msb)
    poller.add_readable_reference(ref_lsb)

    class FakeMaster:
        def __init__(self):
            self.called = None

        def read_input_registers(self, address, *, count=1, device_id=1):
            self.called = ("ir", address, count)
            return FakeResult(registers=[0x8001])  # 1000 0000 0000 0001

    master = FakeMaster()

    assert poller.poll(master) is True
    assert master.called == ("ir", 40000, 1)
    assert ref_msb.val is True
    assert ref_lsb.val is True


def test_bit_reference_respects_endianness():
    device = Device("dev", 1)
    poller = Poller(device, 4, 0, 1, "LE_BE")
    ref_bit15 = Reference(device, "bit15", "0:15", "bool", "r", None, None)
    ref_bit7 = Reference(device, "bit7", "0:7", "bool", "r", None, None)
    poller.add_readable_reference(ref_bit15)
    poller.add_readable_reference(ref_bit7)

    class FakeMaster:
        def __init__(self):
            self.called = None

        def read_input_registers(self, address, *, count=1, device_id=1):
            self.called = ("ir", address, count)
            return FakeResult(registers=[0x8001])  # raw register value

    master = FakeMaster()

    assert poller.poll(master) is True
    assert master.called == ("ir", 0, 1)
    # LE byte order swaps bytes: 0x8001 -> 0x0180 (bit7 set, bit15 clear)
    assert ref_bit15.val is False
    assert ref_bit7.val is True


def test_bit_syntax_only_allowed_with_bool_dtype():
    import pytest

    device = Device("dev", 1)

    # Should work: bool dtype with bit syntax
    ref_bool = Reference(device, "bit_ref", "40000:5", "bool", "r", None, None)
    assert ref_bool.bit == 5

    # Should fail: uint16 dtype with bit syntax
    with pytest.raises(ValueError, match="can only be used with dtype 'bool'"):
        Reference(device, "uint_ref", "40000:5", "uint16", "r", None, None)

    # Should fail: int32 dtype with bit syntax
    with pytest.raises(ValueError, match="can only be used with dtype 'bool'"):
        Reference(device, "int_ref", "40000:0", "int32", "r", None, None)

    # Should work: uint16 dtype without bit syntax
    ref_uint = Reference(device, "uint_ref", "40000", "uint16", "r", None, None)
    assert ref_uint.bit is None


def test_coil_bool_reads_absolute_address():
    device = Device("dev", 1)
    poller = Poller(device, 1, 0, 16, "BE_BE")
    ref = Reference(device, "coil5", "5", "bool", "r", None, None)
    poller.add_readable_reference(ref)
    bits = [False] * 16
    bits[5] = True

    class FakeMaster:
        def read_coils(self, address, *, count=1, device_id=1):
            return FakeResult(bits=bits)

    assert poller.poll(FakeMaster()) is True
    assert ref.val is True


def test_discrete_input_bool_reads_absolute_address():
    device = Device("dev", 1)
    poller = Poller(device, 2, 10000, 16, "BE_BE")
    ref = Reference(device, "di5", "10005", "bool", "r", None, None)
    poller.add_readable_reference(ref)
    bits = [False] * 16
    bits[5] = True

    class FakeMaster:
        def read_discrete_inputs(self, address, *, count=1, device_id=1):
            assert address == 10000
            return FakeResult(bits=bits)

    assert poller.poll(FakeMaster()) is True
    assert ref.val is True


def test_coil_bool_with_nonzero_poll_start():
    device = Device("dev", 1)
    poller = Poller(device, 1, 14, 8, "BE_BE")
    ref = Reference(device, "coil17", "17", "bool", "r", None, None)
    poller.add_readable_reference(ref)
    bits = [False, False, False, True]

    class FakeMaster:
        def read_coils(self, address, *, count=1, device_id=1):
            assert address == 14
            return FakeResult(bits=bits)

    assert poller.poll(FakeMaster()) is True
    assert ref.val is True


def test_bool8_legacy_group_addressing_on_coils():
    device = Device("dev", 1)
    poller = Poller(device, 1, 0, 16, "BE_BE")
    ref = Reference(device, "coil09-16", "1", "bool8", "r", None, None)
    poller.add_readable_reference(ref)
    bits = [False] * 8 + [True] * 8

    class FakeMaster:
        def read_coils(self, address, *, count=1, device_id=1):
            return FakeResult(bits=bits)

    assert poller.poll(FakeMaster()) is True
    assert ref.val == [True] * 8


def test_bool8_legacy_group_addressing_on_discrete_inputs():
    device = Device("dev", 1)
    poller = Poller(device, 2, 10000, 16, "BE_BE")
    ref = Reference(device, "di09-16", "10001", "bool8", "r", None, None)
    poller.add_readable_reference(ref)
    bits = [False] * 8 + [True] * 8

    class FakeMaster:
        def read_discrete_inputs(self, address, *, count=1, device_id=1):
            return FakeResult(bits=bits)

    assert poller.poll(FakeMaster()) is True
    assert ref.val == [True] * 8


def test_bool16_legacy_group_addressing_on_coils():
    device = Device("dev", 1)
    poller = Poller(device, 1, 0, 16, "BE_BE")
    ref = Reference(device, "coil01-16", "0", "bool16", "r", None, None)
    poller.add_readable_reference(ref)
    bits = [True] * 8 + [False] * 8

    class FakeMaster:
        def read_coils(self, address, *, count=1, device_id=1):
            return FakeResult(bits=bits)

    assert poller.poll(FakeMaster()) is True
    assert ref.val == bits[:16]


def test_bool16_partial_last_group_is_padded():
    device = Device("dev", 1)
    poller = Poller(device, 1, 0, 12, "BE_BE")
    ref = Reference(device, "coil01-12", "0", "bool16", "r", None, None)
    poller.add_readable_reference(ref)
    bits = [True] * 12

    class FakeMaster:
        def read_coils(self, address, *, count=1, device_id=1):
            return FakeResult(bits=bits)

    assert poller.poll(FakeMaster()) is True
    assert ref.val == bits + [False] * 4


def test_bool8_partial_last_group_is_padded():
    device = Device("dev", 1)
    poller = Poller(device, 1, 0, 12, "BE_BE")
    ref = Reference(device, "coil09-12", "1", "bool8", "r", None, None)
    poller.add_readable_reference(ref)
    bits = [False] * 8 + [True, True, True, False]

    class FakeMaster:
        def read_coils(self, address, *, count=1, device_id=1):
            return FakeResult(bits=bits)

    assert poller.poll(FakeMaster()) is True
    assert ref.val == [True, True, True, False, False, False, False, False]


def test_coil_bool_and_bool8_can_coexist_in_same_poll():
    device = Device("dev", 1)
    poller = Poller(device, 1, 0, 16, "BE_BE")
    ref_bool = Reference(device, "coil5", "5", "bool", "r", None, None)
    ref_block = Reference(device, "coil00-07", "0", "bool8", "r", None, None)
    poller.add_readable_reference(ref_bool)
    poller.add_readable_reference(ref_block)
    bits = [True, False, False, False, False, True, False, False] + [False] * 8

    class FakeMaster:
        def read_coils(self, address, *, count=1, device_id=1):
            return FakeResult(bits=bits)

    assert poller.poll(FakeMaster()) is True
    assert ref_bool.val is True
    assert ref_block.val == bits[:8]


def test_bit_syntax_rejected_on_coil_poller_during_config_load():
    import io

    config = "\n".join(
        [
            "device,dev,1",
            "poll,coil,0,8,BE_BE",
            "ref,invalid,5:0,bool,r",
        ]
    )
    handler = ModbusHandler(MagicMock(), "unused")
    devices = handler._parse_config(csv.reader(io.StringIO(config)))
    assert len(devices) == 1
    assert len(devices[0].pollerList) == 1
    assert len(devices[0].pollerList[0].readableReferences) == 0
    assert len(devices[0].references) == 0


def test_tab_delimited_config_loads_with_tab_code():
    import io

    config = "\n".join(
        [
            "device\tdev\t1",
            "poll\tholding_register\t0\t1\tBE_BE",
            "ref\tv\t0\tuint16\tr",
        ]
    )
    handler = ModbusHandler(MagicMock(), "unused", csv_delimiter_code="tab")
    devices = handler._parse_config(
        csv.reader(io.StringIO(config), delimiter=handler.csv_delimiter)
    )
    assert len(devices) == 1
    assert len(devices[0].pollerList) == 1
    assert "v" in devices[0].references


def test_load_config_hints_csv_delimiter_on_misparsed_file(caplog, tmp_path):
    config_path = tmp_path / "bad.csv"
    config_path.write_text("device\tdev\t1\n", encoding="utf-8")
    handler = ModbusHandler(MagicMock(), str(config_path), csv_delimiter_code="comma")
    with caplog.at_level("ERROR"):
        assert handler.load_config() is False
    assert "--csv-delimiter" in caplog.text
    assert "comma, tab" in caplog.text
