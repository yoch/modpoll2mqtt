from __future__ import annotations

import json
import logging
import math
import time
from argparse import Namespace
from collections.abc import Iterator

from prettytable import PrettyTable
from pymodbus.client import ModbusSerialClient, ModbusTcpClient, ModbusUdpClient
from pymodbus.exceptions import ModbusException
from pymodbus.framer import FramerType

from .configuration import ConfigurationError, parse_config, read_config, safe_source
from .export import export_snapshot
from .modbus_models import Device, Poller, Reference
from .mqtt_task import MqttHandler
from .reference_common import find_device as _find_device
from .reference_read import read_references as _read_references
from .reference_write import validate_write
from .reference_write import write_reference as _write_reference
from .types import (
    ModbusClient,
    ModbusConnectionDiagnostics,
    ModbusValue,
    MqttPayload,
)
from .utils import delay_thread, on_threading_event

FLOAT_TYPE_PRECISION = 3
MQTT_GLOBAL_DIAGNOSTICS_TOPIC = "modpoll/diagnostics"

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
        self.recover_after = 0.0
        self.bit_write_strategy = "read-modify-write"
        self.read_only = False
        self.command_expires_at: float | None = None
        self.last_write_results: dict[str, dict[str, str]] = {}
        self.deviceList: list[Device] = []
        self.logger = logging.getLogger(__name__)

    def load_config(self) -> bool:
        self.logger.info("Loading config from: %s", safe_source(self.config_file))
        try:
            self.deviceList = read_config(
                self.config_file, self.csv_delimiter, self.timeout, self.logger
            )
            _validate_cross_device_config(self.deviceList, self.logger)
            return True
        except ConfigurationError as exc:
            self.logger.error("%s", exc)
            self.deviceList = []
            return False

    def _parse_config(self, csv_reader: Iterator[list[str]]) -> list[Device]:
        try:
            devices = parse_config(csv_reader, self.config_file, self.logger)
            _validate_cross_device_config(devices, self.logger)
            return devices
        except ConfigurationError as exc:
            self.logger.error("%s", exc)
            return []

    def begin_poll_cycle(self) -> None:
        for dev in self.deviceList:
            dev.pollSuccess = False
            for ref in dev.references.values():
                ref.invalidate()

    def _maybe_disable_poller(self, dev: Device, poller: Poller) -> None:
        if self.autoremove and poller.failcounter >= 3:
            poller.disabled = True
            if self.recover_after > 0:
                poller.retry_at = time.monotonic() + self.recover_after
            self.logger.warning(
                f"Disabled poller for device {dev.name} "
                f"(fc={poller.fc}, start={poller.start_address}) "
                f"after 3 consecutive failures"
            )

    def on_poll_unavailable(self) -> None:
        self.begin_poll_cycle()

    def poll(self) -> bool:
        all_success = True
        attempted = False
        self.begin_poll_cycle()
        for dev in self.deviceList:
            self.logger.debug(f"Polling device {dev.name} ...")
            for p in dev.pollerList:
                if (
                    p.disabled
                    and p.retry_at is not None
                    and time.monotonic() >= p.retry_at
                ):
                    p.disabled = False
                    p.retry_at = None
                    p.failcounter = 0
                if not p.disabled:
                    try:
                        attempted = True
                        if not p.poll(self.modbus_client):
                            all_success = False
                    except (ModbusException, OSError):
                        p.update_statistics(False)
                        for ref in p.readableReferences:
                            ref.update_value(None)
                        self._maybe_disable_poller(dev, p)
                        raise
                    self._maybe_disable_poller(dev, p)
                    if on_threading_event():
                        return attempted and all_success
                    delay_thread(timeout=self.interval)
                else:
                    all_success = False
        if not self.no_output:
            self.print_results()
        return attempted and all_success

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
        self.last_write_results = {
            name: {"status": "rejected", "error": "unknown_reference"}
            for name in ref_values
        }
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

        self.last_write_results = {
            name: {"status": "not_attempted"} for name in ref_values
        }
        rejected = False
        ranges: list[tuple[int, int, int, int | None]] = []
        for ref_name, value in writes:
            try:
                if self.read_only:
                    raise ValueError("Gateway is read-only")
                _, ref, poller = validate_write(self, device_name, ref_name, value)
                start = (
                    poller.start_address + (ref.address - poller.start_address) * 8
                    if poller.fc == 1 and ref.dtype in ("bool8", "bool16")
                    else ref.address
                )
                width = (
                    (8 if ref.dtype == "bool8" else 16)
                    if poller.fc == 1 and ref.dtype in ("bool8", "bool16")
                    else ref.ref_width
                )
                for fc, left, right, bit in ranges:
                    if (
                        fc == poller.fc
                        and start < right
                        and left < start + width
                        and not (
                            ref.bit is not None and bit is not None and bit != ref.bit
                        )
                    ):
                        raise ValueError("Overlapping writes in one command")
                ranges.append((poller.fc, start, start + width, ref.bit))
            except (ValueError, OverflowError) as exc:
                rejected = True
                self.last_write_results[ref_name] = {
                    "status": "rejected",
                    "error": str(exc),
                }
        for name in ref_values:
            if name not in dev.references:
                self.last_write_results[name] = {
                    "status": "rejected",
                    "error": "unknown_reference",
                }
        if rejected:
            dev.setErrors += 1
            dev.setUnknownRefs += unknown_skips
            return
        ok_count = 0
        for i, (ref_name, value) in enumerate(writes):
            if on_threading_event():
                break
            if (
                self.command_expires_at is not None
                and time.time() >= self.command_expires_at
            ):
                for name, _ in writes[i:]:
                    self.last_write_results[name] = {
                        "status": "not_attempted",
                        "error": "command_expired",
                    }
                break
            try:
                ok = self.write_reference(device_name, ref_name, value)
            except (ModbusException, OSError):
                self.last_write_results[ref_name] = {
                    "status": "unknown",
                    "error": "transport_failure",
                }
                raise
            self.last_write_results[ref_name] = {
                "status": "confirmed" if ok else "failed"
            }
            if ok:
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
                "references": {
                    ref.name: {
                        "quality": ref.quality,
                        "last_success_at": ref.last_success_at,
                        "last_attempt_at": ref.last_attempt_at,
                    }
                    for ref in dev.references.values()
                },
                "disabled_pollers": sum(p.disabled for p in dev.pollerList),
                "get_count": dev.getCount,
                "get_errors": dev.getErrors,
                "get_success": dev.getSuccess,
                "get_unknown_refs": dev.getUnknownRefs,
                "get_read_errors": dev.getReadErrors,
                "set_count": dev.setCount,
                "set_errors": dev.setErrors,
                "set_success": dev.setSuccess,
                "set_unknown_refs": dev.setUnknownRefs,
                "config_source": safe_source(self.config_file),
            }
            topic = self.mqtt_diagnostics_topic_pattern.replace(
                "{{device_name}}", dev.name
            )
            self.mqtt_handler.publish(topic, json.dumps(payload), retain=retain)

    def export(self, file: str, timestamp: float | None = None) -> None:
        try:
            export_snapshot(file, self.deviceList, timestamp)
        except (OSError, ValueError) as exc:
            self.logger.error("Error exporting data: %s", exc)

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
        "mqtt_dropped_messages": mqtt_handler.dropped_messages,
        "mqtt_expired_messages": mqtt_handler.expired_messages,
        "mqtt_publish_errors": mqtt_handler.publish_errors,
        "mqtt_subscription_errors": mqtt_handler.subscription_errors,
        **(connection_diagnostics or {}),
    }
    mqtt_handler.publish(
        mqtt_handler.status_topic.rsplit("/", 1)[0] + "/diagnostics",
        json.dumps(payload),
        retain=mqtt_handler.retain_data_publishes,
    )


def setup_modbus_handlers(
    args: Namespace, mqtt_handler: MqttHandler | None = None
) -> tuple[ModbusClient, list[ModbusHandler]]:
    modbus_handlers: list[ModbusHandler] = []
    try:
        modbus_client = (
            ModbusTcpClient("localhost", retries=0)
            if args.check_config
            else _create_modbus_client(args)
        )
    except ValueError as exc:
        raise ConfigurationError(str(exc)) from exc
    names: set[str] = set()
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
        modbus_handler.recover_after = args.poller_recover_after
        modbus_handler.bit_write_strategy = args.modbus_bit_write
        modbus_handler.read_only = args.read_only
        if not modbus_handler.load_config():
            raise ConfigurationError(
                f"Invalid configuration: {safe_source(config_file)}"
            )
        for device in modbus_handler.deviceList:
            if device.name in names:
                raise ConfigurationError(
                    f"Duplicate device name across files: {device.name}"
                )
            if args.serial and device.devid > 247:
                raise ConfigurationError("Serial device IDs must be 1..247")
            names.add(device.name)
            keys = [
                _mqtt_payload_key(ref, args.mqtt_keys)
                for ref in device.references.values()
            ]
            if len(keys) != len(set(keys)) or (args.timestamp and "timestamp" in keys):
                raise ConfigurationError(f"MQTT payload key collision on {device.name}")
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
            retries=0,
            framer=framer,
        )
    return ModbusSerialClient(
        port=port,
        baudrate=int(args.serial_baud),
        bytesize=8,
        parity=parity,
        stopbits=1,
        timeout=args.timeout,
        retries=0,
    )


def _create_tcp_client(args: Namespace, framer: FramerType | None) -> ModbusTcpClient:
    if framer:
        return ModbusTcpClient(
            host=args.tcp,
            port=args.tcp_port,
            timeout=args.timeout,
            retries=0,
            framer=framer,
        )
    return ModbusTcpClient(
        host=args.tcp, port=args.tcp_port, timeout=args.timeout, retries=0
    )


def _create_udp_client(args: Namespace, framer: FramerType | None) -> ModbusUdpClient:
    if framer:
        return ModbusUdpClient(
            host=args.udp,
            port=args.udp_port,
            timeout=args.timeout,
            retries=0,
            framer=framer,
        )
    return ModbusUdpClient(
        host=args.udp, port=args.udp_port, timeout=args.timeout, retries=0
    )


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
