from __future__ import annotations

import json
import logging
import re
import signal
import time
from argparse import Namespace
from functools import partial
from pathlib import Path
from types import FrameType

from . import __version__
from .arg_parser import get_parser
from .commands import RequestCache, decode_json, parse_command
from .configuration import ConfigurationError
from .export import export_snapshot
from .modbus_connection import ModbusConnectionManager
from .modbus_task import (
    ModbusHandler,
    WriteResults,
    format_mqtt_payload_values,
    load_configurations,
    publish_global_diagnostics,
    setup_modbus_handlers,
)
from .mqtt_task import MqttHandler
from .types import is_modbus_value_map
from .utils import (
    clear_threading_event,
    delay_thread,
    get_utc_time,
    on_threading_event,
    set_threading_event,
)

LOG_SIMPLE = "%(asctime)s | %(levelname).1s | %(name)s | %(message)s"
logger: logging.Logger | None = None


def _signal_handler(signum: int, frame: FrameType | None) -> None:
    set_threading_event()


def extract_device_from_mqtt_topic(pattern: str, topic: str) -> str | None:
    parts = pattern.split("+")
    if len(parts) != 2:
        raise ValueError("MQTT subscribe pattern requires one '+' wildcard")
    match = re.fullmatch("([^/\\n]+)".join(re.escape(p) for p in parts), topic)
    return match.group(1) if match else None


def classify_mqtt_command_topic(
    set_pattern: str, get_pattern: str, topic: str
) -> tuple[str | None, str | None]:
    for kind, pattern in (("get", get_pattern), ("set", set_pattern)):
        device = extract_device_from_mqtt_topic(pattern, topic)
        if device is not None:
            return kind, device
    return None, None


def setup_logging(level: str | int, log_format: str) -> None:
    logging.basicConfig(level=level, format=log_format)


def _handle_command(
    args: Namespace,
    mqtt: MqttHandler,
    handlers: list[ModbusHandler],
    manager: ModbusConnectionManager,
    cache: RequestCache,
    topic: str,
    payload: bytes,
) -> None:
    log = logging.getLogger(__name__)
    device_name = extract_device_from_mqtt_topic(args.mqtt_command_topic_pattern, topic)
    if device_name is not None:
        kind = "command"
    else:
        kind, device_name = classify_mqtt_command_topic(
            args.mqtt_subscribe_topic_pattern, args.mqtt_get_topic_pattern, topic
        )
    if not device_name or kind is None:
        log.warning("Unknown command topic: %s", topic)
        return
    response_topic = topic + "/response"
    identifier: object = None
    try:
        if kind == "command":
            identifier = decode_json(payload, args.mqtt_max_payload_bytes).get(
                "request_id"
            )
            if isinstance(identifier, str):
                previous = cache.lookup(device_name, identifier, payload)
                if previous is not None:
                    mqtt.publish_response(response_topic, previous)
                    return
        command = parse_command(
            payload, kind, args.mqtt_max_payload_bytes, args.mqtt_command_ttl
        )
    except (ValueError, OverflowError) as exc:
        log.warning("Rejected command on %s: %s", topic, exc)
        if kind == "command":
            mqtt.publish_response(
                response_topic,
                json.dumps(
                    {
                        "version": 1,
                        "request_id": identifier
                        if isinstance(identifier, str)
                        else None,
                        "status": "rejected",
                        "error": str(exc),
                    }
                ),
            )
        return
    if not command.values:
        log.warning("Empty MQTT %s payload", kind)
        return
    handler = next((h for h in handlers if h.has_device(device_name)), None)
    outcomes: dict[str, dict[str, object]] = {}
    values = {}
    if handler is None:
        outcomes = {
            name: {"status": "rejected", "error": "unknown_device"}
            for name in command.values
        }
    elif command.kind == "set":
        if args.read_only:
            outcomes = {
                name: {"status": "rejected", "error": "read_only"}
                for name in command.values
            }
        else:
            # The envelope is strict; the legacy map continues to skip unknown names.
            dev = next(d for d in handler.deviceList if d.name == device_name)
            if command.request_id and any(
                name not in dev.references for name in command.values
            ):
                outcomes = {
                    name: {
                        "status": "rejected",
                        "error": "unknown_reference_in_command",
                    }
                    for name in command.values
                }
            else:
                results: WriteResults = {
                    name: {"status": "not_attempted", "error": "transport_unavailable"}
                    for name in command.values
                }
                result = manager.execute(
                    "write",
                    partial(
                        handler.write_references,
                        device_name,
                        command.values,
                        results,
                        command.expires_at,
                    ),
                )
                if not result.ok:
                    if result.callback_started:
                        handler.record_set_transport_failure(device_name)
                    else:
                        handler.record_set_unavailable(device_name)
                outcomes = {name: dict(entry) for name, entry in results.items()}
    else:
        result = manager.execute(
            "get", partial(handler.read_references, device_name, list(command.values))
        )
        if not result.ok:
            if result.callback_started:
                handler.record_get_transport_failure(device_name, len(command.values))
            else:
                handler.record_get_unavailable(device_name)
        elif is_modbus_value_map(result.value):
            values = format_mqtt_payload_values(result.value)
        outcomes = {
            name: {"status": "confirmed", "value": values[name]}
            if name in values
            else {"status": "failed", "error": "unreadable_or_unavailable"}
            for name in command.values
        }
    if command.request_id is not None:
        response = json.dumps(
            {
                "version": 1,
                "request_id": command.request_id,
                "operation": command.kind,
                "deduplication_scope": "process",
                "results": outcomes,
            },
            allow_nan=False,
        )
        cache.save(device_name, command.request_id, payload, response)
    elif command.kind == "get":
        response = json.dumps(values, allow_nan=False)
    else:
        response = json.dumps({"results": outcomes}, allow_nan=False)
    mqtt.publish_response(response_topic, response)


def app(name: str = "modpoll") -> None:
    print(f"\nmodpoll2mqtt v{__version__} - Modbus to MQTT gateway\n", flush=True)
    args = get_parser().parse_args()
    setup_logging(args.loglevel, LOG_SIMPLE)
    global logger
    logger = logging.getLogger(__name__)
    clear_threading_event()
    previous_signals = {
        sig: signal.signal(sig, _signal_handler)
        for sig in (signal.SIGINT, signal.SIGTERM)
    }
    mqtt: MqttHandler | None = None
    manager: ModbusConnectionManager | None = None
    failed = False
    try:
        if args.check_config:
            logger.info("Validated %s configuration(s)", len(load_configurations(args)))
            return
        client, handlers = setup_modbus_handlers(args)
        manager = ModbusConnectionManager(
            client,
            backoff_base=args.modbus_backoff_base,
            backoff_max=args.modbus_backoff_max,
            max_connection_age=args.modbus_max_connection_age,
        )
        if args.mqtt_host:
            password = args.mqtt_pass
            if args.mqtt_pass_file:
                password = (
                    Path(args.mqtt_pass_file).read_text(encoding="utf-8").rstrip("\r\n")
                )
            prefix = (
                args.mqtt_namespace.strip("/") if args.mqtt_namespace else "modpoll"
            )
            mqtt = MqttHandler(
                "MqttHandler",
                args.mqtt_host,
                args.mqtt_port,
                args.mqtt_user,
                password,
                args.mqtt_clientid,
                args.mqtt_qos,
                subscribe_topics=[]
                if args.once
                else (
                    [args.mqtt_get_topic_pattern, args.mqtt_command_topic_pattern]
                    + ([] if args.read_only else [args.mqtt_subscribe_topic_pattern])
                ),
                use_tls=args.mqtt_use_tls,
                cacerts=args.mqtt_cacerts,
                insecure=args.mqtt_insecure,
                mqtt_version=args.mqtt_version,
                log_level=args.loglevel,
                rx_queue_size=args.mqtt_rx_queue_size,
                retain_data_publishes=args.mqtt_retain,
                status_topic=prefix + "/status",
                max_payload_bytes=args.mqtt_max_payload_bytes,
                queue_bytes=args.mqtt_queue_bytes,
                command_ttl=args.mqtt_command_ttl,
                tx_queue_size=args.mqtt_tx_queue_size,
                shutdown_timeout=args.mqtt_shutdown_timeout,
                session_expiry=args.mqtt_session_expiry,
            )
            if not mqtt.setup() or not (mqtt.connect() if args.once else mqtt.start()):
                raise RuntimeError("MQTT setup failed")
            for handler in handlers:
                handler.mqtt_handler = mqtt
            if args.mqtt_on_change and args.mqtt_qos == 0:
                logger.warning(
                    "--mqtt-on-change with QoS 0: lost publications cannot be "
                    "detected; use --mqtt-qos 1 so unchanged values are re-sent "
                    "until acknowledged."
                )
            if args.mqtt_on_change and not args.mqtt_retain:
                logger.info(
                    "--mqtt-on-change without --mqtt-retain: consumers that "
                    "connect later only receive values that change afterwards."
                )
        delay_thread(args.delay)
        next_poll = time.monotonic()
        next_diag = next_poll
        last_start: float | None = None
        last_cycle_s = 0.0
        last_transport_ok = False
        cache = RequestCache(ttl=max(300.0, args.mqtt_command_ttl))
        while not on_threading_event():
            now = time.monotonic()
            if now >= next_poll:
                stamp = get_utc_time()
                last_cycle_s = 0.0 if last_start is None else now - last_start
                last_start = now
                last_transport_ok = True
                # Invalidate all snapshots before a cycle that might abort partway through.
                for handler in handlers:
                    handler.begin_poll_cycle()
                for handler in handlers:
                    result = manager.execute("poll", handler.poll)
                    if not result.ok:
                        last_transport_ok = False
                    if not result.ok or result.value is False:
                        failed = True
                    if on_threading_event():
                        break
                if not on_threading_event():
                    for handler in handlers:
                        handler.publish_data(
                            timestamp=stamp if args.timestamp else None
                        )
                    if args.export:
                        try:
                            export_snapshot(
                                args.export,
                                [d for h in handlers for d in h.deviceList],
                                stamp if args.timestamp else None,
                            )
                        except (OSError, ValueError) as exc:
                            logger.error("Error exporting data: %s", exc)
                            failed = True
                next_poll += args.rate
                finish = time.monotonic()
                if next_poll <= finish:
                    logger.warning("Polling overrun: %.3fs", finish - next_poll)
                    next_poll = finish + args.rate
                if args.once:
                    if mqtt and (mqtt.publish_errors or not mqtt.flush()):
                        failed = True
                    break
            if args.diagnostics_rate > 0 and now >= next_diag:
                for handler in handlers:
                    handler.publish_diagnostics()
                if mqtt:
                    publish_global_diagnostics(
                        mqtt,
                        handlers,
                        last_transport_ok,
                        last_cycle_s,
                        manager.diagnostics(),
                    )
                next_diag = time.monotonic() + args.diagnostics_rate
            if mqtt and not on_threading_event():
                topic, payload = mqtt.receive()
                if topic and payload is not None:
                    _handle_command(
                        args, mqtt, handlers, manager, cache, topic, payload
                    )
                    continue
            wait = max(
                0.0,
                min(next_poll, next_diag if args.diagnostics_rate > 0 else next_poll)
                - time.monotonic(),
            )
            if mqtt:
                mqtt.wait(wait)
            else:
                delay_thread(min(wait, 0.1))
    except ConfigurationError as exc:
        logger.error("%s", exc)
        raise SystemExit(2) from exc
    except (ValueError, OSError, RuntimeError) as exc:
        logger.error("Gateway failed: %s", exc)
        raise SystemExit(1) from exc
    except Exception:
        logger.exception("Unexpected gateway failure")
        raise SystemExit(1) from None
    finally:
        try:
            if manager:
                manager.close("shutdown")
        finally:
            try:
                if mqtt:
                    try:
                        mqtt.close()
                    except Exception as exc:
                        logger.warning("MQTT cleanup failed: %s", exc)
            finally:
                for sig, previous in previous_signals.items():
                    signal.signal(sig, previous)
    if args.once and failed:
        raise SystemExit(1)


if __name__ == "__main__":
    app()
