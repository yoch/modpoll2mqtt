from modpoll.modbus_models import Device, Poller, Reference
from modpoll.register_decode import Endian, RegisterDecoder


class FakeResult:
    def __init__(self, *, bits=None, registers=None):
        self.bits = bits
        self.registers = registers

    def isError(self):
        return False


def test_poller_decodes_le_be_bit_and_float16():
    device = Device("dev", 1)
    poller = Poller(device, 3, 40000, 2, "LE_BE")
    ref_msb = Reference(device, "bit15", "40000:15", "bool", "r", None, None)
    ref_float = Reference(device, "current", "40001", "float16", "r", None, None)
    poller.add_readable_reference(ref_msb)
    poller.add_readable_reference(ref_float)

    class FakeMaster:
        def read_holding_registers(self, *args, **kwargs):
            return FakeResult(registers=[0x8001, 0x4228])

    assert poller.poll(FakeMaster()) is True
    assert ref_msb.val is False
    assert ref_float.val == 0.03326416015625


def test_decode_string_be_be():
    decoder = RegisterDecoder.from_registers(
        [0x4142, 0x4344], byteorder=Endian.BIG, wordorder=Endian.BIG
    )
    assert decoder.decode_string(4) == b"ABCD"


def test_decode_string_le_be_swaps_bytes_within_words():
    decoder = RegisterDecoder.from_registers(
        [0x4142, 0x4344], byteorder=Endian.LITTLE, wordorder=Endian.BIG
    )
    assert decoder.decode_string(4) == b"BADC"


def test_poller_decodes_string8_with_endian():
    device = Device("dev", 1)
    poller = Poller(device, 3, 0, 2, "LE_BE")
    ref = Reference(device, "label", "0", "string4", "r", None, None)
    poller.add_readable_reference(ref)

    class FakeMaster:
        def read_holding_registers(self, *args, **kwargs):
            return FakeResult(registers=[0x4142, 0x4344])

    assert poller.poll(FakeMaster()) is True
    assert ref.val == "BADC"
