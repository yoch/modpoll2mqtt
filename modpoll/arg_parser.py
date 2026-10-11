from __future__ import annotations

import math
from argparse import ArgumentParser, Namespace
from collections.abc import Iterable
from typing import cast

from . import __version__

_CSV_DELIMITER_CHOICES = ("comma", "tab")
_MQTT_KEYS_CHOICES = ("name-with-unit", "name-only")

_INTERVAL_DEFAULT_TCP = 0.0
_INTERVAL_DEFAULT_UDP = 0.0
_RTU_BITS_PER_CHARACTER = 11
_RTU_FRAME_GAP_CHARACTERS = 3.5
_RTU_MIN_AUTO_INTERVAL = 0.005


def _default_rtu_interval(serial_baud: int) -> float:
    if serial_baud <= 0:
        return _RTU_MIN_AUTO_INTERVAL
    frame_gap = _RTU_FRAME_GAP_CHARACTERS * _RTU_BITS_PER_CHARACTER / serial_baud
    return max(frame_gap, _RTU_MIN_AUTO_INTERVAL)


def _default_interval_for_transport(args: Namespace) -> float:
    if args.serial:
        return _default_rtu_interval(args.serial_baud)
    if args.udp:
        return _INTERVAL_DEFAULT_UDP
    return _INTERVAL_DEFAULT_TCP


class ModpollArgumentParser(ArgumentParser):
    def parse_args(  # pyright: ignore[reportIncompatibleMethodOverride]
        self,
        args: Iterable[str] | None = None,
        namespace: Namespace | None = None,
    ) -> Namespace:
        parsed = cast(Namespace, super().parse_args(args, namespace))
        return self._validate_timing_options(parsed)

    def parse_known_args(  # pyright: ignore[reportIncompatibleMethodOverride]
        self,
        args: Iterable[str] | None = None,
        namespace: Namespace | None = None,
    ) -> tuple[Namespace, list[str]]:
        parsed, extras = super().parse_known_args(args, namespace)
        return self._validate_timing_options(cast(Namespace, parsed)), extras

    def _validate_timing_options(self, parsed: Namespace) -> Namespace:
        if parsed.serial_baud <= 0:
            self.error("--serial-baud must be greater than 0")
        if parsed.interval is None:
            parsed.interval = _default_interval_for_transport(parsed)
        if parsed.interval < 0:
            self.error("--interval must be greater than or equal to 0")
        if parsed.modbus_backoff_base < 0:
            self.error("--modbus-backoff-base must be greater than or equal to 0")
        if parsed.modbus_backoff_max < parsed.modbus_backoff_base:
            self.error(
                "--modbus-backoff-max must be greater than or equal to "
                "--modbus-backoff-base"
            )
        if (
            parsed.modbus_max_connection_age is not None
            and parsed.modbus_max_connection_age <= 0
        ):
            self.error("--modbus-max-connection-age must be greater than 0")
        for key in (
            "rate",
            "timeout",
            "delay",
            "diagnostics_rate",
            "interval",
            "modbus_backoff_base",
            "modbus_backoff_max",
            "poller_recover_after",
            "mqtt_command_ttl",
            "mqtt_shutdown_timeout",
        ):
            value = getattr(parsed, key)
            if (
                not math.isfinite(value)
                or value < 0
                or (key in ("rate", "timeout", "mqtt_command_ttl") and value == 0)
            ):
                self.error(
                    f"--{key.replace('_', '-')} must be finite and {'positive' if key in ('rate', 'timeout', 'mqtt_command_ttl') else 'nonnegative'}"
                )
        if parsed.modbus_max_connection_age is not None and not math.isfinite(
            parsed.modbus_max_connection_age
        ):
            self.error("--modbus-max-connection-age must be finite")
        for key in ("tcp_port", "udp_port", "mqtt_port"):
            if not 1 <= getattr(parsed, key) <= 65535:
                self.error(f"--{key.replace('_', '-')} must be 1..65535")
        for key in (
            "mqtt_rx_queue_size",
            "mqtt_max_payload_bytes",
            "mqtt_queue_bytes",
            "mqtt_tx_queue_size",
        ):
            if getattr(parsed, key) < 1:
                self.error(f"--{key.replace('_', '-')} must be positive")
        if parsed.mqtt_pass_file and parsed.mqtt_pass:
            self.error("Use either --mqtt-pass-file or --mqtt-pass")
        if parsed.mqtt_session_expiry > 4294967295:
            self.error("--mqtt-session-expiry exceeds MQTT uint32 range")
        if parsed.mqtt_tls_version != "tlsv1.2":
            self.error("TLS 1.0/1.1 are unsupported; use --mqtt-tls-version tlsv1.2")
        if parsed.mqtt_session_expiry < 0:
            self.error("--mqtt-session-expiry must be nonnegative")
        if parsed.mqtt_session_expiry and not parsed.mqtt_clientid:
            self.error("A persistent MQTT session requires --mqtt-clientid")
        if parsed.mqtt_namespace:
            prefix = parsed.mqtt_namespace.strip("/")
            if (
                not prefix
                or any(c in prefix for c in "+#\x00")
                or any(ord(c) < 32 for c in prefix)
            ):
                self.error("Invalid --mqtt-namespace")
            for key in (
                "mqtt_publish_topic_pattern",
                "mqtt_subscribe_topic_pattern",
                "mqtt_get_topic_pattern",
                "mqtt_diagnostics_topic_pattern",
                "mqtt_command_topic_pattern",
            ):
                value = getattr(parsed, key)
                if value.startswith("modpoll/"):
                    setattr(parsed, key, prefix + value[len("modpoll") :])
        patterns = [
            parsed.mqtt_subscribe_topic_pattern,
            parsed.mqtt_get_topic_pattern,
            parsed.mqtt_command_topic_pattern,
        ]
        for pattern in patterns:
            if (
                pattern.split("/").count("+") != 1
                or "#" in pattern
                or any("+" in part and part != "+" for part in pattern.split("/"))
                or not pattern
                or any(ord(c) < 32 for c in pattern)
                or len(pattern.encode("utf-8")) > 65535
            ):
                self.error(
                    "Command topic patterns require exactly one complete '+' device segment"
                )
        for index, left in enumerate(patterns):
            for right in patterns[index + 1 :]:
                left_parts, right_parts = left.split("/"), right.split("/")
                if len(left_parts) == len(right_parts) and all(
                    a == b or "+" in (a, b) for a, b in zip(left_parts, right_parts)
                ):
                    self.error(
                        "Command topic patterns must be distinct and non-overlapping"
                    )
        for pattern in (
            parsed.mqtt_publish_topic_pattern,
            parsed.mqtt_diagnostics_topic_pattern,
        ):
            if (
                any(c in pattern for c in "+#\x00")
                or not pattern
                or any(ord(c) < 32 for c in pattern)
                or len(pattern.encode("utf-8")) > 65535
            ):
                self.error("Invalid MQTT publish topic pattern")
        return parsed


def get_parser() -> ModpollArgumentParser:
    parser = ModpollArgumentParser(
        description=f"modpoll2mqtt v{__version__} - Modbus to MQTT gateway"
    )
    parser.add_argument(
        "-v",
        "--version",
        action="version",
        version=f"v{__version__}",
    )
    parser.add_argument(
        "-f",
        "--config",
        required=True,
        help="A local path or URL of Modbus configuration file. Required!",
        nargs="+",
    )
    parser.add_argument(
        "--csv-delimiter",
        default="comma",
        choices=_CSV_DELIMITER_CHOICES,
        help=(
            "Column delimiter code for Modbus config files "
            f"({', '.join(_CSV_DELIMITER_CHOICES)}). Defaults to comma"
        ),
    )
    parser.add_argument(
        "--no-output",
        action="store_true",
        help="Do not print poll results to stdout (useful under systemd)",
    )
    parser.add_argument(
        "-r",
        "--rate",
        type=float,
        default=10.0,
        help="The sampling rate (s) to poll modbus device, Defaults to 10.0",
    )
    parser.add_argument(
        "-1", "--once", action="store_true", help="Only run polling at one time"
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=None,
        help=(
            "Delay in seconds between pollers and between references in one MQTT "
            "set message. Defaults automatically by transport: 0.0 for TCP/UDP, or "
            "the RTU frame gap derived from --serial-baud with a 0.005s floor for "
            "serial/RTU"
        ),
    )
    parser.add_argument(
        "--tcp", help="Act as a Modbus TCP master, connecting to host TCP"
    )
    parser.add_argument(
        "--tcp-port", type=int, default=502, help="Port for MODBUS TCP. Defaults to 502"
    )
    parser.add_argument(
        "--udp", help="Act as a Modbus UDP master, connecting to host UDP"
    )
    parser.add_argument(
        "--udp-port", type=int, default=502, help="Port for MODBUS UDP. Defaults to 502"
    )
    parser.add_argument(
        "--serial",
        "--rtu",
        dest="serial",
        help="pyserial URL (or port name) for serial transport (alias: --rtu)",
    )
    parser.add_argument(
        "--serial-baud",
        "--rtu-baud",
        dest="serial_baud",
        type=int,
        default=9600,
        help="Baud rate for serial port. Defaults to 9600",
    )
    parser.add_argument(
        "--serial-parity",
        "--rtu-parity",
        dest="serial_parity",
        choices=["none", "odd", "even"],
        default="none",
        help="Parity for serial port. Defaults to none",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=3.0,
        help="Response time-out seconds for MODBUS devices, Defaults to 3.0",
    )
    parser.add_argument(
        "--modbus-backoff-base",
        type=float,
        default=1.0,
        help="Initial Modbus reconnect backoff in seconds. Defaults to 1.0",
    )
    parser.add_argument(
        "--modbus-backoff-max",
        type=float,
        default=60.0,
        help="Maximum Modbus reconnect backoff in seconds. Defaults to 60.0",
    )
    parser.add_argument(
        "--modbus-max-connection-age",
        type=float,
        default=None,
        help="Maximum age in seconds for a persistent Modbus connection before recycling. Disabled by default",
    )
    parser.add_argument(
        "-o",
        "--export",
        default=None,
        help="The file name to export references/registers",
    )
    parser.add_argument(
        "--mqtt-version",
        choices=["3.1.1", "5.0"],
        default="3.1.1",
        help="MQTT version. Defaults to MQTT v3.1.1",
    )
    parser.add_argument(
        "--mqtt-host",
        default=None,
        help="MQTT server address. Skip MQTT setup if not specified",
    )
    parser.add_argument(
        "--mqtt-port",
        type=int,
        default=1883,
        help="1883 for non-TLS or 8883 for TLS, Defaults to 1883",
    )
    parser.add_argument(
        "--mqtt-clientid",
        default=None,
        help="MQTT client name, If qos > 0, set unique name for multiple clients",
    )
    parser.add_argument(
        "--mqtt-publish-topic-pattern",
        default="modpoll/{{device_name}}/data",
        help='Topic pattern for MQTT publish. Use {{device_name}} as placeholder for the device names in Modbus config. Defaults to "modpoll/{{device_name}}/data"',
    )
    parser.add_argument(
        "--mqtt-subscribe-topic-pattern",
        default="modpoll/+/set",
        help='Topic pattern for MQTT write commands. Use + as placeholder for device name. Defaults to "modpoll/+/set"',
    )
    parser.add_argument(
        "--mqtt-get-topic-pattern",
        default="modpoll/+/get",
        help='Topic pattern for MQTT on-demand read commands. Use + as placeholder for device name. Defaults to "modpoll/+/get"',
    )
    parser.add_argument(
        "--mqtt-diagnostics-topic-pattern",
        default="modpoll/{{device_name}}/diagnostics",
        help="Topic pattern for MQTT diagnostics. Use {{device_name}} as placeholder for the device names in Modbus config. Defaults to modpoll/{{device_name}}/diagnostics",
    )
    parser.add_argument(
        "--mqtt-qos",
        choices=[0, 1, 2],
        type=int,
        default=0,
        help="MQTT QoS value. Defaults to 0",
    )
    parser.add_argument(
        "--mqtt-rx-queue-size",
        type=int,
        default=1000,
        metavar="N",
        help="Max MQTT subscribe messages buffered between polls (default: 1000)",
    )
    parser.add_argument(
        "--mqtt-user", default=None, help="Username for authentication (optional)"
    )
    parser.add_argument(
        "--mqtt-pass", default=None, help="Password for authentication (optional)"
    )
    parser.add_argument("--mqtt-use-tls", action="store_true", help="Use TLS")
    parser.add_argument(
        "--mqtt-insecure",
        action="store_true",
        help="Use TLS without providing certificates",
    )
    parser.add_argument("--mqtt-cacerts", default=None, help="Path to ca keychain")
    parser.add_argument(
        "--mqtt-tls-version",
        choices=["tlsv1.2", "tlsv1.1", "tlsv1"],
        default="tlsv1.2",
        help="TLS protocol version, can be one of tlsv1.2 tlsv1.1 or tlsv1",
    )
    parser.add_argument(
        "--mqtt-single",
        action="store_true",
        help="Publish each value in a single topic. If not specified, groups all values in one topic.",
    )
    parser.add_argument(
        "--mqtt-keys",
        default="name-with-unit",
        choices=_MQTT_KEYS_CHOICES,
        help=(
            'MQTT JSON payload key format. "name-with-unit" appends the unit suffix when '
            'configured (default). "name-only" uses reference names only.'
        ),
    )
    parser.add_argument(
        "--mqtt-retain",
        action="store_true",
        help="Set the MQTT retain flag on published data messages.",
    )
    parser.add_argument(
        "--diagnostics-rate",
        type=float,
        default=0,
        help="Time in seconds as publishing period for each device diagnostics",
    )
    parser.add_argument(
        "--autoremove",
        action="store_true",
        help="Automatically remove poller if modbus communication has failed 3 times.",
    )
    parser.add_argument(
        "--loglevel",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        default="INFO",
        help="Set log level, Defaults to INFO",
    )
    parser.add_argument(
        "--timestamp", action="store_true", help="Add timestamp to the result"
    )
    parser.add_argument(
        "--delay",
        type=int,
        default=0,
        help="Time to delay sending first request in seconds after connecting. Default to 0",
    )
    parser.add_argument(
        "--framer",
        default="default",
        choices=["default", "ascii", "rtu", "socket"],
        help="The type of framer for Modbus messages. Serial supports ascii/rtu; TCP/UDP use socket.",
    )
    parser.add_argument(
        "--check-config",
        action="store_true",
        help="Validate all CSV files without opening network or serial transports",
    )
    parser.add_argument(
        "--read-only", action="store_true", help="Disable all MQTT write commands"
    )
    parser.add_argument(
        "--poller-recover-after",
        type=float,
        default=0.0,
        help="Probe disabled pollers after N seconds; 0 keeps permanent --autoremove behavior",
    )
    parser.add_argument(
        "--modbus-bit-write",
        choices=("read-modify-write", "mask"),
        default="read-modify-write",
        help="Use FC22 atomic mask writes when supported, or the legacy non-atomic read-modify-write",
    )
    parser.add_argument(
        "--mqtt-namespace",
        default=None,
        help="Prefix for all default data, command, status and diagnostic topics",
    )
    parser.add_argument(
        "--mqtt-command-topic-pattern",
        default="modpoll/+/commands",
        help="Versioned get/set command envelope topic",
    )
    parser.add_argument(
        "--mqtt-max-payload-bytes",
        type=int,
        default=65536,
        help="Maximum bytes in one incoming command",
    )
    parser.add_argument(
        "--mqtt-queue-bytes",
        type=int,
        default=1048576,
        help="Maximum total payload bytes in the incoming queue",
    )
    parser.add_argument(
        "--mqtt-tx-queue-size",
        type=int,
        default=1000,
        help="Maximum outgoing MQTT messages buffered by Paho",
    )
    parser.add_argument(
        "--mqtt-command-ttl",
        type=float,
        default=30.0,
        help="Maximum incoming queue age and default command lifetime in seconds",
    )
    parser.add_argument(
        "--mqtt-shutdown-timeout",
        type=float,
        default=3.0,
        help="Maximum seconds to flush MQTT publishes at shutdown",
    )
    parser.add_argument(
        "--mqtt-session-expiry",
        type=int,
        default=0,
        help="Persistent session lifetime (seconds); requires an explicit client ID",
    )
    parser.add_argument(
        "--mqtt-pass-file",
        default=None,
        help="Read MQTT password from a protected UTF-8 file instead of an argument",
    )
    return parser
