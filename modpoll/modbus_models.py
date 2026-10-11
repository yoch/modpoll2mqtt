"""Core Modbus device, poller, and reference models."""

from __future__ import annotations

import logging
import math
import re
import struct
from typing import Any

from .reference_common import call_with_device_id as _call_with_device_id
from .register_decode import ENDIAN_MAP, RegisterDecoder
from .types import ModbusClient, ModbusValue, is_numeric_modbus_value
from .utils import get_utc_time


class Device:
    def __init__(self, device_name: str, device_id: int) -> None:
        self.name = device_name
        self.devid = device_id
        self.pollerList: list[Poller] = []
        self.references: dict[str, Reference] = {}
        self.errorCount = 0
        self.pollCount = 0
        self.pollSuccess = False
        self.getCount = 0
        self.getErrors = 0
        self.getSuccess = 0
        self.getUnknownRefs = 0
        self.getReadErrors = 0
        self.setCount = 0
        self.setErrors = 0
        self.setSuccess = 0
        self.setUnknownRefs = 0

    def add_reference_mapping(self, ref: Reference) -> None:
        self.references[ref.name] = ref


class Poller:
    def __init__(
        self,
        device: Device,
        function_code: int,
        start_address: int,
        size: int,
        endian: str,
    ) -> None:
        self.device = device
        self.fc = function_code
        self.start_address = start_address
        self.size = size
        self.endian = endian.lower()
        self.readableReferences: list[Reference] = []
        self.disabled = False
        self.failcounter = 0
        self.retry_at: float | None = None
        self.logger = logging.getLogger(__name__)

    def poll(self, master: ModbusClient) -> bool:
        result = None

        def _call_read(method: Any) -> Any:
            return _call_with_device_id(
                method,
                self.start_address,
                count=self.size,
                device_id=self.device.devid,
            )

        if self.fc == 1:
            result = _call_read(master.read_coils)
        elif self.fc == 2:
            result = _call_read(master.read_discrete_inputs)
        elif self.fc == 3:
            result = _call_read(master.read_holding_registers)
        elif self.fc == 4:
            result = _call_read(master.read_input_registers)

        if result is None or result.isError():
            self.update_statistics(False)
            for ref in self.readableReferences:
                ref.update_value(None)
            return False

        block = result.bits if self.fc in (1, 2) else result.registers
        expected = self.size
        if block is None or len(block) < expected:
            self.update_statistics(False)
            for ref in self.readableReferences:
                ref.update_value(None)
            return False
        successes = 0
        failures = 0
        for ref in self.readableReferences:
            try:
                value = (
                    self.decode_coil_bits(ref, block[: self.size], self.start_address)
                    if self.fc in (1, 2)
                    else self.decode_register_block(ref, block, self.start_address)
                )
                ref.update_value(value)
                if ref.val is None:
                    failures += 1
                else:
                    successes += 1
                if self.is_partial_group(ref):
                    ref.quality = "partial"
            except (ValueError, IndexError, struct.error, OverflowError) as exc:
                ref.update_value(None)
                failures += 1
                self.logger.error("Failed to decode reference %s: %s", ref.name, exc)
        self.update_statistics(failures == 0)
        if successes:
            self.device.pollSuccess = True
        return failures == 0

    def coil_group_start(self, ref: Reference) -> int:
        """First coil of a bool8/bool16 group; groups are 8 coils apart."""
        return self.start_address + (ref.address - self.start_address) * 8

    def is_partial_group(self, ref: Reference) -> bool:
        """True when a coil bool8/bool16 group extends past this poller."""
        return (
            self.fc in (1, 2)
            and ref.dtype in ("bool8", "bool16")
            and self.coil_group_start(ref) + ref.group_width
            > self.start_address + self.size
        )

    def get_decoder(self, data: list[int]) -> RegisterDecoder:
        byteorder, wordorder = ENDIAN_MAP[self.endian.strip().upper()]
        return RegisterDecoder.from_registers(
            data, byteorder=byteorder, wordorder=wordorder
        )

    def decode_coil_bits(
        self, ref: Reference, bits: list[bool], read_start: int
    ) -> ModbusValue:
        """Decode a single reference from a coil/discrete_input read.

        ``read_start`` is the Modbus address corresponding to ``bits[0]``.
        bool8/bool16 groups shorter than 8/16 bits are padded with False.
        """
        if ref.dtype == "bool" and ref.bit is None:
            bit_offset = ref.address - read_start
            return bool(bits[bit_offset])
        if ref.dtype in ("bool8", "bool16"):
            bit_offset = self.coil_group_start(ref) - read_start
            values = bits[bit_offset : bit_offset + ref.group_width]
            return values + [False] * (ref.group_width - len(values))
        raise ValueError(
            f"Unsupported dtype '{ref.dtype}' on coil/discrete_input poller"
        )

    def decode_register_block(
        self, ref: Reference, registers: list[int], read_start: int
    ) -> ModbusValue:
        """Decode a single reference from a register read starting at ``read_start``."""
        offset_bytes = (ref.address - read_start) * 2
        if offset_bytes < 0:
            raise ValueError(
                f"Reference {ref.name} address {ref.address} is outside read "
                f"block starting at {read_start}"
            )
        decoder = self.get_decoder(registers)
        decoder.skip_bytes(offset_bytes)
        return self._decode_register_value(ref, decoder)

    def _decode_register_value(
        self, ref: Reference, decoder: RegisterDecoder
    ) -> ModbusValue:
        if ref.dtype == "bool" and ref.bit is not None:
            register_value = decoder.decode_16bit_uint()
            return bool((register_value >> ref.bit) & 1)

        if ref.dtype in ("bool8", "bool16"):
            word = decoder.decode_16bit_uint()
            return [bool((word >> i) & 1) for i in range(ref.group_width)]

        decode_methods = {
            "uint16": decoder.decode_16bit_uint,
            "int16": decoder.decode_16bit_int,
            "uint32": decoder.decode_32bit_uint,
            "int32": decoder.decode_32bit_int,
            "uint64": decoder.decode_64bit_uint,
            "int64": decoder.decode_64bit_int,
            "float16": decoder.decode_16bit_float,
            "float32": decoder.decode_32bit_float,
            "float64": decoder.decode_64bit_float,
        }

        if ref.dtype in decode_methods:
            return decode_methods[ref.dtype]()
        if ref.dtype.startswith("string"):
            return (
                decoder.decode_string(ref.ref_width * 2)[: ref.string_bytes]
                .decode("utf-8")
                .rstrip("\x00")
            )
        raise ValueError(f"Unsupported dtype '{ref.dtype}' for register decode")

    def add_readable_reference(self, ref: Reference) -> None:
        ref.poller = self
        if not any(existing is ref for existing in self.readableReferences):
            self.readableReferences.append(ref)

    def update_statistics(self, success: bool) -> None:
        self.device.pollCount += 1
        if success:
            self.failcounter = 0
            self.device.pollSuccess = True
        else:
            self.failcounter += 1
            self.device.errorCount += 1


class Reference:
    def __init__(
        self,
        device: Device,
        ref_name: str,
        ref_addr: str,
        dtype: str,
        rw: str,
        unit: str | None,
        scale: float | None,
    ) -> None:
        self.device = device
        self.poller: Poller | None = None
        self.name = ref_name
        self.bit: int | None = None
        try:
            if ":" in ref_addr:
                addr, bit = ref_addr.split(":")
                self.address = int(addr, 0)
                self.bit = int(bit)
                if not 0 <= self.bit <= 15:
                    raise ValueError("Bit index must be between 0 and 15")
            else:
                self.address = int(ref_addr, 0)
        except ValueError as e:
            raise ValueError(f"Invalid address format for {ref_name}: {e}") from e
        self.dtype = dtype.lower()
        # Validate that bit syntax is only used with bool dtype
        if self.bit is not None and self.dtype != "bool":
            raise ValueError(
                f"Bit index syntax (address:bit) can only be used with dtype 'bool', "
                f"but reference {ref_name} has dtype '{self.dtype}'"
            )
        if self.dtype not in {
            "bool",
            "bool8",
            "bool16",
            "int16",
            "uint16",
            "int32",
            "uint32",
            "int64",
            "uint64",
            "float16",
            "float32",
            "float64",
        } and not re.fullmatch(r"string[1-9][0-9]*", self.dtype):
            raise ValueError(f"Unsupported dtype: {dtype}")
        self.string_bytes = (
            int(self.dtype[6:]) if self.dtype.startswith("string") else 0
        )
        self.ref_width = self._get_ref_width()
        self.rw = rw.lower()
        self.unit = unit
        if scale is not None and (not math.isfinite(scale) or scale == 0):
            raise ValueError("Scale must be finite and nonzero")
        self.scale = scale
        self.val: ModbusValue = None
        self.last_success_at: float | None = None
        self.last_attempt_at: float | None = None
        self.quality = "unavailable"

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Reference):
            return (
                self.address == other.address
                and self.bit == other.bit
                and self.dtype == other.dtype
            )
        return False

    def __hash__(self):
        return hash((self.address, self.bit, self.dtype))

    def __repr__(self):
        addr = f"{self.address}:{self.bit}" if self.bit is not None else self.address
        return f"<Reference {self.name}@{addr}>"

    @property
    def group_width(self) -> int:
        """Bit count of a bool8/bool16 group."""
        return 8 if self.dtype == "bool8" else 16

    def _get_ref_width(self) -> int:
        width_map = {
            "int16": 1,
            "uint16": 1,
            "float16": 1,
            "bool8": 1,
            "bool": 1,
            "int32": 2,
            "uint32": 2,
            "float32": 2,
            "bool16": 1,
            "int64": 4,
            "uint64": 4,
            "float64": 4,
        }
        if self.dtype in width_map:
            return width_map[self.dtype]
        elif self.dtype.startswith("string"):
            try:
                width = int(self.dtype[6:])
                return (width + 1) // 2
            except ValueError:
                return 1
        else:
            return 1

    def check_sanity(self, reference: int, size: int, fc: int | None = None) -> bool:
        if fc in (1, 2):
            if self.dtype == "bool" and self.bit is None:
                return self.address in range(reference, size + reference)
            if self.dtype in ("bool8", "bool16"):
                group_offset = self.address - reference
                return group_offset >= 0 and group_offset * 8 < size
            return False
        return self.address in range(
            reference, size + reference
        ) and self.address + self.ref_width - 1 in range(reference, size + reference)

    def invalidate(self) -> None:
        """Discard the current snapshot without claiming a bus read took place."""
        self.val = None
        self.quality = "unavailable"

    def update_value(self, v: ModbusValue) -> None:
        self.last_attempt_at = get_utc_time()
        if self.scale is not None and is_numeric_modbus_value(v):
            v = v * self.scale
        if isinstance(v, float) and not math.isfinite(v):
            v = None
        self.val = v
        self.quality = "good" if v is not None else "unavailable"
        if v is not None:
            self.last_success_at = self.last_attempt_at
