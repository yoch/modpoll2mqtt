"""Register and coil payload decoding (replaces removed pymodbus.payload).

Derived from pymodbus 3.9.2 BinaryPayloadDecoder.
"""

from __future__ import annotations

from array import array
from struct import pack, unpack
from typing import Literal

ByteOrder = Literal[">", "<"]


class Endian:
    BIG: ByteOrder = ">"
    LITTLE: ByteOrder = "<"


ENDIAN_MAP: dict[str, tuple[ByteOrder, ByteOrder]] = {
    "BE_BE": (Endian.BIG, Endian.BIG),
    "LE_BE": (Endian.LITTLE, Endian.BIG),
    "LE_LE": (Endian.LITTLE, Endian.LITTLE),
    "BE_LE": (Endian.BIG, Endian.LITTLE),
}


class RegisterDecoder:
    """Decode Modbus register/coil payloads with configurable byte and word order."""

    __slots__ = ("_payload", "_pointer", "_byteorder", "_wordorder")

    def __init__(
        self,
        payload: bytes,
        byteorder: ByteOrder = Endian.LITTLE,
        wordorder: ByteOrder = Endian.BIG,
    ):
        self._payload = payload
        self._pointer = 0
        self._byteorder = byteorder
        self._wordorder = wordorder

    @classmethod
    def from_registers(
        cls,
        registers: list[int],
        byteorder: ByteOrder = Endian.LITTLE,
        wordorder: ByteOrder = Endian.BIG,
    ) -> RegisterDecoder:
        payload = pack(f"!{len(registers)}H", *registers)
        return cls(payload, byteorder, wordorder)

    def _unpack_words(self, handle: bytes) -> bytes:
        if Endian.LITTLE in (self._byteorder, self._wordorder):
            handle_array = array("H", handle)
            if self._byteorder == Endian.LITTLE:
                handle_array.byteswap()
            if self._wordorder == Endian.LITTLE:
                handle_array.reverse()
            handle = handle_array.tobytes()
        return handle

    def decode_16bit_uint(self) -> int:
        self._pointer += 2
        handle = self._payload[self._pointer - 2 : self._pointer]
        return unpack(self._byteorder + "H", handle)[0]

    def decode_16bit_int(self) -> int:
        self._pointer += 2
        handle = self._payload[self._pointer - 2 : self._pointer]
        return unpack(self._byteorder + "h", handle)[0]

    def decode_32bit_uint(self) -> int:
        self._pointer += 4
        handle = self._unpack_words(self._payload[self._pointer - 4 : self._pointer])
        return unpack("!I", handle)[0]

    def decode_32bit_int(self) -> int:
        self._pointer += 4
        handle = self._unpack_words(self._payload[self._pointer - 4 : self._pointer])
        return unpack("!i", handle)[0]

    def decode_64bit_uint(self) -> int:
        self._pointer += 8
        handle = self._unpack_words(self._payload[self._pointer - 8 : self._pointer])
        return unpack("!Q", handle)[0]

    def decode_64bit_int(self) -> int:
        self._pointer += 8
        handle = self._unpack_words(self._payload[self._pointer - 8 : self._pointer])
        return unpack("!q", handle)[0]

    def decode_16bit_float(self) -> float:
        self._pointer += 2
        handle = self._unpack_words(self._payload[self._pointer - 2 : self._pointer])
        return unpack("!e", handle)[0]

    def decode_32bit_float(self) -> float:
        self._pointer += 4
        handle = self._unpack_words(self._payload[self._pointer - 4 : self._pointer])
        return unpack("!f", handle)[0]

    def decode_64bit_float(self) -> float:
        self._pointer += 8
        handle = self._unpack_words(self._payload[self._pointer - 8 : self._pointer])
        return unpack("!d", handle)[0]

    def decode_string(self, size: int = 1) -> bytes:
        self._pointer += size
        handle = self._payload[self._pointer - size : self._pointer]
        if size >= 2:
            handle = self._unpack_words(handle)
        return handle

    def skip_bytes(self, nbytes: int) -> None:
        self._pointer += nbytes


class RegisterEncoder:
    """Encode Python values into Modbus register words (inverse of RegisterDecoder)."""

    __slots__ = ("_payload", "_byteorder", "_wordorder")

    def __init__(
        self, byteorder: ByteOrder = Endian.LITTLE, wordorder: ByteOrder = Endian.BIG
    ):
        self._payload = bytearray()
        self._byteorder = byteorder
        self._wordorder = wordorder

    def _pack_words(self, handle: bytes) -> bytes:
        if Endian.LITTLE in (self._byteorder, self._wordorder):
            if len(handle) >= 2:
                handle_array = array("H", handle)
                if self._wordorder == Endian.LITTLE:
                    handle_array.reverse()
                if self._byteorder == Endian.LITTLE:
                    handle_array.byteswap()
                handle = handle_array.tobytes()
        return handle

    def encode_16bit_uint(self, value: int) -> None:
        self._payload.extend(pack(self._byteorder + "H", value & 0xFFFF))

    def encode_16bit_int(self, value: int) -> None:
        self._payload.extend(pack(self._byteorder + "h", value))

    def encode_32bit_uint(self, value: int) -> None:
        handle = pack("!I", value & 0xFFFFFFFF)
        self._payload.extend(self._pack_words(handle))

    def encode_32bit_int(self, value: int) -> None:
        handle = pack("!i", value)
        self._payload.extend(self._pack_words(handle))

    def encode_64bit_uint(self, value: int) -> None:
        handle = pack("!Q", value & 0xFFFFFFFFFFFFFFFF)
        self._payload.extend(self._pack_words(handle))

    def encode_64bit_int(self, value: int) -> None:
        handle = pack("!q", value)
        self._payload.extend(self._pack_words(handle))

    def encode_16bit_float(self, value: float) -> None:
        handle = pack("!e", value)
        self._payload.extend(self._pack_words(handle))

    def encode_32bit_float(self, value: float) -> None:
        handle = pack("!f", value)
        self._payload.extend(self._pack_words(handle))

    def encode_64bit_float(self, value: float) -> None:
        handle = pack("!d", value)
        self._payload.extend(self._pack_words(handle))

    def encode_string(self, value: str, size: int) -> None:
        encoded = value.encode("utf-8")[:size]
        encoded = encoded.ljust(size, b"\x00")
        handle = encoded
        if size >= 2:
            handle = self._pack_words(handle)
        self._payload.extend(handle)

    def to_registers(self) -> list[int]:
        payload = bytes(self._payload)
        if len(payload) % 2:
            payload += b"\x00"
        if not payload:
            return []
        return list(unpack(f"!{len(payload) // 2}H", payload))
