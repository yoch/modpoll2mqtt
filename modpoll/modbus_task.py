from __future__ import annotations

import csv
import json
import logging
import math
from argparse import Namespace
from collections.abc import Iterator
from typing import Any

import requests
from prettytable import PrettyTable
from pymodbus.client import ModbusSerialClient, ModbusTcpClient, ModbusUdpClient
from pymodbus.exceptions import ModbusException
from pymodbus.framer import FramerType

from .mqtt_task import MqttHandler
from .reference_common import call_with_device_id as _call_with_device_id
from .reference_common import find_device as _find_device
from .reference_read import read_references as _read_references
from .reference_write import write_reference as _write_reference
from .register_decode import ENDIAN_MAP, RegisterDecoder
from .types import (
    ModbusClient,
    ModbusConnectionDiagnostics,
    ModbusValue,
    MqttPayload,
    is_numeric_modbus_value,
)
from .utils import delay_thread, on_threading_event

FLOAT_TYPE_PRECISION = 3
MQTT_GLOBAL_DIAGNOSTICS_TOPIC = "modpoll/diagnostics"
CONFIG_DEVICE_COL_MIN = 3
CONFIG_POLL_COL_MIN = 5
CONFIG_REF_COL_MIN = 5

CSV_DELIMITER_CODES = {
    "comma": ",",
    "tab": "\t",
}
_MQTT_KEYS_NAME_WITH_UNIT = "name-with-unit"
_MQTT_KEYS_NAME_ONLY = "name-only"


def _mqtt_payload_key(ref: Reference, mqtt_keys: str) -> str:
    if mqtt_keys == _MQTT_KEYS_NAME_ONLY:
        return ref.name
    if mqtt_keys == _MQTT_KEYS_NAME_WITH_UNIT:
        return f"{ref.name}|{ref.unit}" if ref.unit else ref.name
    raise ValueError(f"Unknown mqtt_keys: {mqtt_keys}")


def format_mqtt_scalar(val: ModbusValue) -> ModbusValue:
    """Return an MQTT-safe scalar, or None to omit (non-finite float)."""
    if isinstance(val, float) and not math.isfinite(val):
        return None
    if isinstance(val, float):
        return round(val, FLOAT_TYPE_PRECISION)
    return val


def format_mqtt_payload_values(
    values: dict[str, ModbusValue],
) -> dict[str, ModbusValue]:
    out: dict[str, ModbusValue] = {}
    for key, val in values.items():
        mqtt_val = format_mqtt_scalar(val)
        if mqtt_val is None and isinstance(val, float):
            continue
        out[key] = mqtt_val
    return out


def _poller_identity(poller: Poller) -> tuple[int, int, int, str]:
    return (poller.fc, poller.start_address, poller.size, poller.endian.upper())


def _validate_cross_device_config(
    device_list: list[Device], logger: logging.Logger
) -> None:
    by_slave: dict[int, list[str]] = {}
    by_slave_fc: dict[tuple[int, int], list[tuple[str, int, int]]] = {}
    for dev in device_list:
        by_slave.setdefault(dev.devid, []).append(dev.name)
        for poller in dev.pollerList:
            key = (dev.devid, poller.fc)
            by_slave_fc.setdefault(key, []).append(
                (dev.name, poller.start_address, poller.size)
            )

    slaves_with_overlap: set[int] = set()
    for (slave_id, fc), pollers in by_slave_fc.items():
        for i, (name_a, start_a, size_a) in enumerate(pollers):
            end_a = start_a + size_a
            for name_b, start_b, size_b in pollers[i + 1 :]:
                end_b = start_b + size_b
                if start_a < end_b and start_b < end_a:
                    slaves_with_overlap.add(slave_id)
                    logger.warning(
                        f"Overlapping Modbus poll ranges on slave ID {slave_id} "
                        f"(fc={fc}): device '{name_a}' [{start_a}, {end_a}) "
                        f"overlaps device '{name_b}' [{start_b}, {end_b})"
                    )

    for slave_id, names in by_slave.items():
        if len(names) > 1 and slave_id not in slaves_with_overlap:
            logger.warning(
                f"Modbus slave ID {slave_id} shared by logical devices: "
                f"{', '.join(names)}"
            )


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


class ModbusHandler:
    def __init__(
        self,
        modbus_client: ModbusClient,
        config_file: str,
        mqtt_handler: MqttHandler | None = None,
        timeout: float = 3.0,
        interval: float = 0.0,
        no_output: bool = False,
        mqtt_publish_topic_pattern: str | None = None,
        mqtt_diagnostics_topic_pattern: str | None = None,
        mqtt_single_publish: bool = False,
        mqtt_keys: str = _MQTT_KEYS_NAME_WITH_UNIT,
        autoremove: bool = False,
        csv_delimiter_code: str = "comma",
    ) -> None:
        self.modbus_client = modbus_client
        self.config_file = config_file
        self.csv_delimiter_code = csv_delimiter_code
        self.csv_delimiter = CSV_DELIMITER_CODES[csv_delimiter_code]
        self.mqtt_handler = mqtt_handler
        self.timeout = timeout
        self.interval = interval
        self.no_output = no_output
        self.mqtt_publish_topic_pattern = mqtt_publish_topic_pattern
        self.mqtt_diagnostics_topic_pattern = mqtt_diagnostics_topic_pattern
        self.mqtt_single_publish = mqtt_single_publish
        self.mqtt_keys = mqtt_keys
        self.autoremove = autoremove
        self.deviceList: list[Device] = []
        self.logger = logging.getLogger(__name__)

    def load_config(self) -> bool:
        self.logger.info(f"Loading config from: {self.config_file}")
        try:
            with requests.Session() as s:
                response = s.get(self.config_file, timeout=self.timeout)
                response.raise_for_status()
                decoded_content = response.content.decode("utf-8")
                csv_reader = csv.reader(
                    decoded_content.splitlines(), delimiter=self.csv_delimiter
                )
                self.deviceList = self._parse_config(csv_reader)
        except requests.RequestException:
            try:
                with open(self.config_file) as f:
                    csv_reader = csv.reader(f, delimiter=self.csv_delimiter)
                    self.deviceList = self._parse_config(csv_reader)
            except OSError as e:
                self.logger.error(f"Error opening file: {e}")
                return False
        if self.deviceList:
            self.logger.info(f"Added {len(self.deviceList)} device(s)...")
            return True
        else:
            self.logger.error(
                "No device found in the config file. Skipping. "
                "If columns are not split correctly, try --csv-delimiter "
                f"({', '.join(sorted(CSV_DELIMITER_CODES))}); "
                f"current: {self.csv_delimiter_code}"
            )
            return False

    def _parse_config(self, csv_reader: Iterator[list[str]]) -> list[Device]:
        device_list: list[Device] = []
        current_device: Device | None = None
        current_poller: Poller | None = None
        seen_device_names: set[str] = set()
        try:
            for row in csv_reader:
                if not row or all(cell.strip() == "" for cell in row):
                    continue
                if "device" in row[0].lower():
                    if len(row) < CONFIG_DEVICE_COL_MIN:
                        self.logger.error("Invalid device configuration")
                        continue
                    device_name = row[1].strip()
                    try:
                        device_id = int(row[2], 0)
                    except ValueError:
                        self.logger.error(f"Invalid device ID for {device_name}")
                        continue
                    if device_name in seen_device_names:
                        self.logger.error(
                            f"Duplicate device name '{device_name}'; aborting config file"
                        )
                        return []
                    seen_device_names.add(device_name)
                    current_device = Device(device_name, device_id)
                    device_list.append(current_device)
                elif "poll" in row[0].lower():
                    if not current_device:
                        self.logger.error("No device to add poller.")
                        continue
                    if len(row) < CONFIG_POLL_COL_MIN:
                        self.logger.error("Invalid poller configuration")
                        return []
                    endian_key = row[4].strip().upper()
                    if endian_key not in ENDIAN_MAP:
                        self.logger.error(
                            f"Invalid endian '{row[4].strip()}'; must be one of: "
                            f"{', '.join(ENDIAN_MAP.keys())}"
                        )
                        return []
                    new_poller = self._create_poller(row, current_device)
                    if new_poller:
                        poller_key = _poller_identity(new_poller)
                        if any(
                            _poller_identity(p) == poller_key
                            for p in current_device.pollerList
                        ):
                            self.logger.warning(
                                f"Duplicate poller on device {current_device.name} "
                                f"(fc={new_poller.fc}, start={new_poller.start_address}, "
                                f"size={new_poller.size}, endian={new_poller.endian}); "
                                f"ignoring it."
                            )
                        else:
                            current_device.pollerList.append(new_poller)
                            current_poller = new_poller
                elif "ref" in row[0].lower():
                    if not current_device or not current_poller:
                        self.logger.debug(
                            f"No device/poller for reference {row[1] if len(row) > 1 else 'unknown'}."
                        )
                        continue
                    ref = self._create_reference(row, current_device)
                    if ref and self._validate_reference(ref, current_poller):
                        if "r" in ref.rw.lower():
                            current_poller.add_readable_reference(ref)
                        if ref.name in current_device.references:
                            self.logger.error(
                                f"Duplicate reference name '{ref.name}' on device "
                                f"{current_device.name}; aborting config file"
                            )
                            return []
                        current_device.add_reference_mapping(ref)
                        self.logger.debug(
                            f"Add reference {ref.name} to device {current_device.name}"
                        )
            _validate_cross_device_config(device_list, self.logger)
            return device_list
        except Exception as e:
            self.logger.error(f"Error parsing config: {e}")
            return []

    def _create_poller(self, row: list[str], current_device: Device) -> Poller | None:
        fc = row[1].lower()
        try:
            start_address = int(row[2], 0)
            size = int(row[3], 0)
        except ValueError:
            self.logger.error("Invalid start address or size for poller")
            return None
        endian = row[4]
        function_code = self._get_function_code(fc)
        if function_code is None:
            return None
        if not self._validate_poller_size(function_code, size):
            return None
        return Poller(current_device, function_code, start_address, size, endian)

    def _get_function_code(self, fc: str) -> int | None:
        fc_map = {
            "coil": 1,
            "discrete_input": 2,
            "holding_register": 3,
            "input_register": 4,
        }
        if fc in fc_map:
            return fc_map[fc]
        self.logger.warning(f"Unknown function code ({fc}) ignoring poller.")
        return None

    def _validate_poller_size(self, function_code: int, size: int) -> bool:
        if size <= 0:
            self.logger.error(
                f"Poller size must be greater than 0: {size}. Ignoring poller."
            )
            return False
        if function_code in (1, 2) and size > 2000:
            self.logger.error(
                f"Too many coils/discrete inputs (max. 2000): {size}. Ignoring poller."
            )
            return False
        if function_code in (3, 4) and size > 123:
            self.logger.error(
                f"Too many registers (max. 123): {size}. Ignoring poller."
            )
            return False
        return True

    def _create_reference(
        self, row: list[str], current_device: Device
    ) -> Reference | None:
        if len(row) < CONFIG_REF_COL_MIN:
            self.logger.error("Invalid reference configuration")
            return None
        ref_name = row[1].replace(" ", "_")
        try:
            address = row[2]
        except ValueError:
            self.logger.error(f"Invalid address for reference {ref_name}")
            return None
        dtype = row[3].lower()
        rw = row[4] or "r"
        unit = row[5] if len(row) > 5 else None
        try:
            scale = float(row[6]) if len(row) > 6 else None
        except ValueError:
            scale = None
        return Reference(current_device, ref_name, address, dtype, rw, unit, scale)

    def _validate_reference(self, ref: Reference, current_poller: Poller) -> bool:
        if current_poller.fc in (1, 2) and ref.bit is not None:
            self.logger.warning(
                f"Reference {ref.name}: address:bit syntax is only supported on "
                f"holding_register/input_register pollers, ignoring it."
            )
            return False
        for poller in current_poller.device.pollerList:
            if ref in poller.readableReferences:
                if poller is current_poller:
                    self.logger.warning(
                        f"Reference {ref.name} is already added, ignoring it."
                    )
                else:
                    self.logger.warning(
                        f"Reference {ref.name} duplicates address/dtype in another "
                        f"poller on device {current_poller.device.name}; ignoring it."
                    )
                return False
        if not ref.check_sanity(
            current_poller.start_address, current_poller.size, current_poller.fc
        ):
            self.logger.warning(
                f"Reference {ref.name} failed to pass sanity check, ignoring it."
            )
            return False
        self._warn_if_read_only_poller_marked_writable(ref, current_poller)
        return True

    def _warn_if_read_only_poller_marked_writable(
        self, ref: Reference, current_poller: Poller
    ) -> None:
        if current_poller.fc not in (2, 4) or "w" not in ref.rw:
            return
        object_type = "discrete_input" if current_poller.fc == 2 else "input_register"
        self.logger.warning(
            f"Reference {ref.name} is marked '{ref.rw}' on an {object_type} poller; "
            "Modbus inputs are read-only and MQTT writes to this reference will be "
            "rejected."
        )

    def _begin_poll_cycle(self) -> None:
        for dev in self.deviceList:
            dev.pollSuccess = False

    def _maybe_disable_poller(self, dev: Device, poller: Poller) -> None:
        if self.autoremove and poller.failcounter >= 3:
            poller.disabled = True
            self.logger.warning(
                f"Disabled poller for device {dev.name} "
                f"(fc={poller.fc}, start={poller.start_address}) "
                f"after 3 consecutive failures"
            )

    def _record_poll_failure(self) -> None:
        for dev in self.deviceList:
            for p in dev.pollerList:
                if not p.disabled:
                    p.update_statistics(False)
                    self._maybe_disable_poller(dev, p)

    def on_poll_unavailable(self) -> None:
        self._begin_poll_cycle()
        self._record_poll_failure()

    def poll(self) -> bool:
        any_success = False
        self._begin_poll_cycle()
        for dev in self.deviceList:
            self.logger.debug(f"Polling device {dev.name} ...")
            for p in dev.pollerList:
                if not p.disabled:
                    try:
                        if p.poll(self.modbus_client):
                            any_success = True
                    except (ModbusException, OSError):
                        p.update_statistics(False)
                        for ref in p.readableReferences:
                            ref.update_value(None)
                        self._maybe_disable_poller(dev, p)
                        raise
                    self._maybe_disable_poller(dev, p)
                    if on_threading_event():
                        return any_success
                    delay_thread(timeout=self.interval)
        if not self.no_output:
            self.print_results()
        return any_success

    def has_device(self, device_name: str) -> bool:
        return any(d.name == device_name for d in self.deviceList)

    def write_reference(
        self, device_name: str, ref_name: str, value: ModbusValue
    ) -> bool:
        return _write_reference(self, device_name, ref_name, value)

    def write_references(
        self,
        device_name: str,
        ref_values: MqttPayload,
    ) -> None:
        dev = _find_device(self, device_name)
        if dev is None:
            return

        dev.setCount += 1
        unknown_skips = 0
        writes: list[tuple[str, ModbusValue]] = []
        for ref_name, value in ref_values.items():
            if ref_name not in dev.references:
                self.logger.warning(
                    f"Unknown reference '{ref_name}' on device {device_name}, skipping"
                )
                unknown_skips += 1
                continue
            writes.append((ref_name, value))

        if not writes:
            self.logger.warning(
                f"No known references in write payload for device={device_name}"
            )
            dev.setUnknownRefs += unknown_skips
            dev.setErrors += 1
            return

        ok_count = 0
        for i, (ref_name, value) in enumerate(writes):
            if self.write_reference(device_name, ref_name, value):
                ok_count += 1
            if i < len(writes) - 1:
                delay_thread(timeout=self.interval)

        dev.setUnknownRefs += unknown_skips
        if unknown_skips or ok_count < len(writes):
            dev.setErrors += 1
        else:
            dev.setSuccess += 1

        if ok_count:
            self.logger.info(f"Wrote {ok_count} value(s) for device={device_name}")

    def read_references(
        self, device_name: str, ref_names: list[str]
    ) -> dict[str, ModbusValue]:
        dev = _find_device(self, device_name)
        if dev is None:
            return {}
        if not ref_names:
            self.logger.warning(f"Empty MQTT get payload for device={device_name}")
            return {}

        dev.getCount += 1
        values, unknown_skips, read_failures = _read_references(self, dev, ref_names)
        dev.getUnknownRefs += unknown_skips
        dev.getReadErrors += read_failures
        if unknown_skips or read_failures:
            dev.getErrors += 1
        else:
            dev.getSuccess += 1
        return values

    def record_get_unavailable(self, device_name: str) -> None:
        dev = _find_device(self, device_name)
        if dev is not None:
            dev.getCount += 1
            dev.getErrors += 1

    def record_get_transport_failure(
        self, device_name: str, failed_ref_count: int = 1
    ) -> None:
        dev = _find_device(self, device_name)
        if dev is not None:
            dev.getErrors += 1
            dev.getReadErrors += max(failed_ref_count, 1)

    def record_set_unavailable(self, device_name: str) -> None:
        dev = _find_device(self, device_name)
        if dev is not None:
            dev.setCount += 1
            dev.setErrors += 1

    def record_set_transport_failure(self, device_name: str) -> None:
        dev = _find_device(self, device_name)
        if dev is not None:
            dev.setErrors += 1

    def print_results(self) -> None:
        for dev in self.deviceList:
            table = PrettyTable()
            table.field_names = ["Reference", "Value", "Unit"]
            table.align["Reference"] = "l"
            table.align["Value"] = "r"
            table.align["Unit"] = "l"
            for ref in dev.references.values():
                value = (
                    round(ref.val, FLOAT_TYPE_PRECISION)
                    if isinstance(ref.val, float)
                    else ref.val
                )
                table.add_row([ref.name, value, ref.unit or ""])
            print(f"\nDevice: {dev.name}")
            print(table)

    def publish_data(self, timestamp: float | None = None) -> None:
        if not self.mqtt_handler or not self.mqtt_publish_topic_pattern:
            return

        for dev in self.deviceList:
            if not dev.pollSuccess:
                self.logger.debug(
                    f"Skip publishing for disconnected device: {dev.name}"
                )
                continue

            payload: dict[str, ModbusValue] = {}
            for ref in dev.references.values():
                if ref.val is None:
                    continue
                ref_val = format_mqtt_scalar(ref.val)
                if ref_val is None:
                    continue
                key = _mqtt_payload_key(ref, self.mqtt_keys)
                payload[key] = ref_val

                if self.mqtt_single_publish:
                    topic = f"{self.mqtt_publish_topic_pattern.replace('{{device_name}}', dev.name)}/{ref.name}"
                    if isinstance(ref_val, list):
                        for i, entry in enumerate(ref_val):
                            msg = json.dumps(entry)
                            self.mqtt_handler.publish_data_message(f"{topic}/{i}", msg)
                    else:
                        msg = (
                            json.dumps(ref_val)
                            if isinstance(ref_val, bool)
                            else str(ref_val)
                        )
                        self.mqtt_handler.publish_data_message(topic, msg)

            if payload and not self.mqtt_single_publish:
                if timestamp is not None:
                    payload["timestamp"] = timestamp
                topic = self.mqtt_publish_topic_pattern.replace(
                    "{{device_name}}", dev.name
                )
                self.mqtt_handler.publish_data_message(topic, json.dumps(payload))

    def publish_diagnostics(self) -> None:
        if not self.mqtt_handler:
            return
        if not self.mqtt_diagnostics_topic_pattern:
            return
        retain = self.mqtt_handler.retain_data_publishes
        for dev in self.deviceList:
            payload = {
                "poll_count": dev.pollCount,
                "error_count": dev.errorCount,
                "last_poll_success": dev.pollSuccess,
                "get_count": dev.getCount,
                "get_errors": dev.getErrors,
                "get_success": dev.getSuccess,
                "get_unknown_refs": dev.getUnknownRefs,
                "get_read_errors": dev.getReadErrors,
                "set_count": dev.setCount,
                "set_errors": dev.setErrors,
                "set_success": dev.setSuccess,
                "set_unknown_refs": dev.setUnknownRefs,
                "config_source": self.config_file,
            }
            topic = self.mqtt_diagnostics_topic_pattern.replace(
                "{{device_name}}", dev.name
            )
            self.mqtt_handler.publish(topic, json.dumps(payload), retain=retain)

    def export(self, file: str, timestamp: float | None = None) -> None:
        data: dict[str, dict[str, ModbusValue]] = {}
        for dev in self.deviceList:
            dev_data = {}
            for ref in dev.references.values():
                if isinstance(ref.val, float) and not math.isfinite(ref.val):
                    continue
                dev_data[ref.name] = ref.val
            if timestamp:
                dev_data["timestamp"] = timestamp
            data[dev.name] = dev_data
        try:
            with open(file, "w") as f:
                json.dump(data, f, indent=2)
        except OSError as e:
            self.logger.error(f"Error exporting data: {e}")

    def get_device_list(self) -> list[Device]:
        return self.deviceList


def count_devices_failing(modbus_handlers: list[ModbusHandler]) -> int:
    return sum(
        1
        for handler in modbus_handlers
        for dev in handler.deviceList
        if not dev.pollSuccess
    )


def publish_global_diagnostics(
    mqtt_handler: MqttHandler,
    modbus_handlers: list[ModbusHandler],
    modbus_ok: bool,
    last_cycle_s: float,
    connection_diagnostics: ModbusConnectionDiagnostics | None = None,
) -> None:
    payload = {
        "mqtt_connected": mqtt_handler.is_connected(),
        "modbus_ok": modbus_ok,
        "devices_failing": count_devices_failing(modbus_handlers),
        "last_cycle_s": last_cycle_s,
        **(connection_diagnostics or {}),
    }
    mqtt_handler.publish(
        MQTT_GLOBAL_DIAGNOSTICS_TOPIC,
        json.dumps(payload),
        retain=mqtt_handler.retain_data_publishes,
    )


def setup_modbus_handlers(
    args: Namespace, mqtt_handler: MqttHandler | None = None
) -> tuple[ModbusClient, list[ModbusHandler]]:
    modbus_handlers: list[ModbusHandler] = []
    modbus_client = _create_modbus_client(args)
    for config_file in args.config:
        modbus_handler = ModbusHandler(
            modbus_client,
            config_file,
            mqtt_handler,
            timeout=args.timeout,
            interval=args.interval,
            no_output=args.no_output,
            mqtt_publish_topic_pattern=args.mqtt_publish_topic_pattern,
            mqtt_diagnostics_topic_pattern=args.mqtt_diagnostics_topic_pattern,
            mqtt_single_publish=args.mqtt_single,
            mqtt_keys=args.mqtt_keys,
            autoremove=args.autoremove,
            csv_delimiter_code=args.csv_delimiter,
        )
        if modbus_handler.load_config():
            modbus_handlers.append(modbus_handler)
    return modbus_client, modbus_handlers


def _create_modbus_client(args: Namespace) -> ModbusClient:
    transport = _determine_transport(args)

    if transport == "serial":
        framer = _resolve_framer("serial", args.framer)
        return _create_serial_client(args, args.serial, framer)

    if transport == "tcp":
        framer = _resolve_framer("tcp", args.framer)
        return _create_tcp_client(args, framer)

    if transport == "udp":
        framer = _resolve_framer("udp", args.framer)
        return _create_udp_client(args, framer)

    raise ValueError("No communication method specified.")


def _create_serial_client(
    args: Namespace, port: str, framer: FramerType | None
) -> ModbusSerialClient:
    if not port:
        raise ValueError("Serial port/URL must be provided for serial transports.")
    parity = _get_parity(args.serial_parity)
    if framer:
        return ModbusSerialClient(
            port=port,
            baudrate=int(args.serial_baud),
            bytesize=8,
            parity=parity,
            stopbits=1,
            timeout=args.timeout,
            framer=framer,
        )
    return ModbusSerialClient(
        port=port,
        baudrate=int(args.serial_baud),
        bytesize=8,
        parity=parity,
        stopbits=1,
        timeout=args.timeout,
    )


def _create_tcp_client(args: Namespace, framer: FramerType | None) -> ModbusTcpClient:
    if framer:
        return ModbusTcpClient(
            host=args.tcp,
            port=args.tcp_port,
            timeout=args.timeout,
            framer=framer,
        )
    return ModbusTcpClient(host=args.tcp, port=args.tcp_port, timeout=args.timeout)


def _create_udp_client(args: Namespace, framer: FramerType | None) -> ModbusUdpClient:
    if framer:
        return ModbusUdpClient(
            host=args.udp,
            port=args.udp_port,
            timeout=args.timeout,
            framer=framer,
        )
    return ModbusUdpClient(host=args.udp, port=args.udp_port, timeout=args.timeout)


def _get_parity(serial_parity: str) -> str:
    if serial_parity == "odd":
        return "O"
    elif serial_parity == "even":
        return "E"
    else:
        return "N"


def _determine_transport(args: Namespace) -> str:
    transports: list[str] = []
    if args.serial:
        transports.append("serial")
    if args.tcp:
        transports.append("tcp")
    if args.udp:
        transports.append("udp")

    if not transports:
        raise ValueError("No communication method specified.")
    if len(transports) > 1:
        raise ValueError(
            "Multiple communication methods specified; pick one of --serial/--tcp/--udp (alias: --rtu)."
        )
    return transports[0]


def _resolve_framer(transport: str, framer_name: str) -> FramerType | None:
    """Resolve the requested framer to a concrete class."""

    if framer_name == "default":
        # Let pymodbus choose its transport defaults:
        # Serial -> RTU framer; TCP/UDP -> socket framer.
        return None

    framer_map = {
        "rtu": FramerType.RTU,
        "ascii": FramerType.ASCII,
        "socket": FramerType.SOCKET,
    }
    allowed = {
        "serial": {"rtu", "ascii"},
        "tcp": {"socket"},
        "udp": {"socket"},
    }

    if transport in ("tcp", "udp"):
        transport_key = transport
    else:
        transport_key = "serial"

    if framer_name not in allowed[transport_key]:
        raise ValueError(
            f"Framer '{framer_name}' is not valid for transport '{transport_key}'."
        )

    framer_type = framer_map.get(framer_name)
    if framer_type is None:
        raise ValueError(
            f"Framer '{framer_name}' is not available with the installed pymodbus version."
        )
    return framer_type
