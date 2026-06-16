"""Core Modbus device, poller, and reference models."""

from __future__ import annotations

import logging
from typing import Any

from .reference_common import call_with_device_id as _call_with_device_id
from .register_decode import ENDIAN_MAP, RegisterDecoder
from .types import ModbusClient, ModbusValue, is_numeric_modbus_value


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
        self.logger = logging.getLogger(__name__)

    def poll(self, master: ModbusClient) -> bool:
        result = None
        data: list[int] | None = None

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

        if result is not None and not result.isError():
            if self.fc in (1, 2):
                bits = result.bits
                if bits is None:
                    self.update_statistics(False)
                    for ref in self.readableReferences:
                        ref.update_value(None)
                    return False
                sorted_refs = sorted(self.readableReferences, key=lambda r: r.address)
                for ref in sorted_refs:
                    try:
                        value = self.decode_coil_bits(ref, bits, self.start_address)
                        ref.update_value(value)
                    except IndexError:
                        self.logger.error(
                            f"Reference {ref.name} address {ref.address} is outside "
                            f"of poller range starting at {self.start_address}"
                        )
                    except Exception:
                        self.logger.error(
                            f"Failed to decode value for reference: {ref.name}"
                        )
            else:
                data = result.registers
                if data is None:
                    self.update_statistics(False)
                    for ref in self.readableReferences:
                        ref.update_value(None)
                    return False
                sorted_refs = sorted(self.readableReferences, key=lambda r: r.address)
                for ref in sorted_refs:
                    try:
                        ref.update_value(
                            self.decode_register_block(ref, data, self.start_address)
                        )
                    except Exception as e:
                        self.logger.error(
                            f"Failed to decode value for reference: {ref.name} - {e}"
                        )
            self.update_statistics(True)
            return True

        self.update_statistics(False)
        for ref in self.readableReferences:
            ref.update_value(None)
        return False

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
            group_offset = ref.address - self.start_address
            bit_offset = group_offset * 8 - (read_start - self.start_address)
            width = 8 if ref.dtype == "bool8" else 16
            values = bits[bit_offset : bit_offset + width]
            return values + [False] * (width - len(values))
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
            width = 8 if ref.dtype == "bool8" else 16
            word = decoder.decode_16bit_uint()
            return [bool((word >> i) & 1) for i in range(width)]

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
                decoder.decode_string(ref.ref_width * 2).decode("utf-8").rstrip("\x00")
            )
        raise ValueError(f"Unsupported dtype '{ref.dtype}' for register decode")

    def add_readable_reference(self, ref: Reference) -> None:
        if ref not in self.readableReferences:
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
        self.ref_width = self._get_ref_width()
        self.rw = rw.lower()
        self.unit = unit
        self.scale = scale
        self.val: ModbusValue = None

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
            "bool16": 2,
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
                width = 16 if self.dtype == "bool16" else 8
                group_offset = self.address - reference
                return group_offset >= 0 and group_offset * width < size
            return False
        return self.address in range(
            reference, size + reference
        ) and self.address + self.ref_width - 1 in range(reference, size + reference)

    def update_value(self, v: ModbusValue) -> None:
        if self.scale and is_numeric_modbus_value(v):
            try:
                v = v * float(self.scale)
            except (ValueError, TypeError):
                pass
        self.val = v
