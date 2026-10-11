"""Regression cases from the 2.2.1 gateway audit."""

import csv
import io
import json
import logging
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from tests.helpers.modbus import FakeModbusMaster, FakeModbusResult

from modpoll import main
from modpoll.arg_parser import get_parser
from modpoll.commands import RequestCache, parse_command
from modpoll.configuration import (
    ConfigurationError,
    parse_config,
    read_config,
    safe_source,
)
from modpoll.export import export_snapshot
from modpoll.modbus_connection import ModbusConnectionManager
from modpoll.modbus_task import ModbusHandler, setup_modbus_handlers
from modpoll.mqtt_task import MqttHandler
from modpoll.register_decode import ENDIAN_MAP, RegisterEncoder
from modpoll.utils import clear_threading_event

CONFIG = "device,dev,1\npoll,holding_register,0,8,BE_BE\n"


def handler_for(text, master=None):
    handler = ModbusHandler(
        master or FakeModbusMaster(registers=[0] * 8), "config.csv", no_output=True
    )
    handler.deviceList = parse_config(
        csv.reader(io.StringIO(text)), "config.csv", logging.getLogger(__name__)
    )
    return handler


@pytest.mark.parametrize(
    "rows",
    [
        "device,other,2\nref,orphan,0,uint16,r\n",
        "poll,unknown,0,1,BE_BE\nref,orphan,0,uint16,r\n",
        "poll,holding_register,65535,2,BE_BE\n",
        "poll,holding_register,0,126,BE_BE\n",
        "poll,holding_register,0,1,WRONG\n",
        "ref,bad,0,unknown,r\n",
        "ref,bad,0,string0,r\n",
        "ref,bad,0:16,bool,r\n",
        "ref,bad,0,bool,r\n",
        "ref,bad,0,uint16,unknown\n",
        "ref,bad,0,uint16,r,,NaN\n",
        "ref,bad,0,uint16,r,,0\n",
        "ref,same,0,uint16,r\nref,same,1,uint16,r\n",
        "device,bad/name,1\n",
        "device,other,0\n",
    ],
)
def test_configuration_fails_atomically_with_line(rows):
    with pytest.raises(ConfigurationError, match=r"config.csv:\d+:"):
        handler_for(CONFIG + "ref,valid,0,uint16,r\n" + rows)


def test_binding_separates_overlapping_function_codes_and_endian():
    master = FakeModbusMaster(registers=[0x0102] * 8)
    master.read_input_registers = MagicMock(
        return_value=FakeModbusResult(registers=[0x0304])
    )
    handler = handler_for(
        CONFIG
        + "ref,holding,0,uint16,rw\npoll,input_register,0,1,LE_BE\nref,input,0,uint16,r\n",
        master,
    )
    assert handler.read_references("dev", ["holding", "input"]) == {
        "holding": 0x0102,
        "input": 0x0403,
    }
    assert handler.write_reference("dev", "input", 5) is False
    assert not master.writes


def test_duplicate_declaration_reuses_poller_but_keeps_reference_aliases():
    handler = handler_for(
        CONFIG
        + "ref,one,0,uint16,r\npoll,holding_register,0,8,BE_BE\nref,two,0,uint16,r\n"
    )
    device = handler.deviceList[0]
    assert len(device.pollerList) == 1
    assert len(device.pollerList[0].readableReferences) == 2
    assert handler.poll()
    assert all(r.val == 0 for r in device.references.values())


@pytest.mark.parametrize(
    "dtype,values",
    [
        ("uint16", [-1, 65536, 1.5]),
        ("int16", [-32769, 32768]),
        ("uint32", [-1, 2**32]),
        ("int32", [-(2**31) - 1, 2**31]),
        ("uint64", [-1, 2**64]),
        ("int64", [-(2**63) - 1, 2**63]),
        ("float16", [1e30]),
        ("float32", [1e100]),
    ],
)
def test_out_of_range_write_never_reaches_bus(dtype, values):
    handler = handler_for(CONFIG + f"ref,value,0,{dtype},rw\n")
    for value in values + [float("nan"), float("inf"), True]:
        assert handler.write_reference("dev", "value", value) is False
    assert not handler.modbus_client.writes


@pytest.mark.parametrize("endian", list(ENDIAN_MAP))
@pytest.mark.parametrize(
    "dtype,value",
    [
        ("uint16", 65535),
        ("int16", -32768),
        ("uint32", 2**32 - 1),
        ("int32", -(2**31)),
        ("uint64", 2**64 - 1),
        ("int64", -(2**63)),
        ("float16", 1.25),
        ("float32", 1.25),
        ("float64", 1.25),
    ],
)
def test_boundary_roundtrip_all_endians(dtype, value, endian):
    handler = handler_for(CONFIG.replace("BE_BE", endian) + f"ref,value,0,{dtype},rw\n")
    assert handler.write_reference("dev", "value", value)
    assert handler.read_references("dev", ["value"]) == {"value": value}


@pytest.mark.parametrize("endian", list(ENDIAN_MAP))
def test_bit_write_inverse_and_mask_preserve_other_bits(endian):
    handler = handler_for(CONFIG.replace("BE_BE", endian) + "ref,bit,0:2,bool,rw\n")
    byteorder, wordorder = ENDIAN_MAP[endian]
    encoder = RegisterEncoder(byteorder=byteorder, wordorder=wordorder)
    encoder.encode_16bit_uint(0xA501)
    handler.modbus_client.registers[0] = encoder.to_registers()[0]
    assert handler.write_reference("dev", "bit", True)
    poller = handler.deviceList[0].pollerList[0]
    assert (
        poller.get_decoder(handler.modbus_client.registers[:1]).decode_16bit_uint()
        == 0xA505
    )
    handler.bit_write_strategy = "mask"
    handler.modbus_client.mask_write_register = MagicMock(
        return_value=FakeModbusResult()
    )
    handler.modbus_client.read_holding_registers = MagicMock(
        side_effect=AssertionError("FC22 must not read")
    )
    assert handler.write_reference("dev", "bit", False)
    call = handler.modbus_client.mask_write_register.call_args.kwargs
    raw_mask = 0x0004 if byteorder == ">" else 0x0400
    assert call["and_mask"] == (~raw_mask & 65535)
    assert call["or_mask"] == 0


def test_odd_utf8_string_reserves_register_and_rejects_truncation():
    handler = handler_for(CONFIG + "ref,label,0,string3,rw\n")
    assert handler.write_reference("dev", "label", "éA")
    assert handler.read_references("dev", ["label"]) == {"label": "éA"}
    assert handler.write_reference("dev", "label", "éé") is False
    assert len(handler.modbus_client.writes) == 1


def test_decode_failure_clears_snapshot_but_preserves_last_success():
    master = FakeModbusMaster(registers=[0x4142, 7] + [0] * 6)
    handler = handler_for(
        CONFIG + "ref,label,0,string2,r\nref,value,1,uint16,r\n", master
    )
    assert handler.poll()
    ref = handler.deviceList[0].references["label"]
    last_success = ref.last_success_at
    master.registers[0] = 0xFFFF
    assert handler.poll() is False
    assert ref.val is None and ref.last_value == "AB"
    assert ref.quality == "unavailable" and ref.last_success_at == last_success
    assert handler.deviceList[0].references["value"].val == 7
    assert handler.deviceList[0].pollSuccess
    attempt = ref.last_attempt_at
    handler.on_poll_unavailable()
    assert ref.last_attempt_at == attempt


def test_short_modbus_response_is_a_poll_failure():
    handler = handler_for(
        CONFIG + "ref,value,0,uint16,r\n", FakeModbusMaster(registers=[1])
    )
    assert handler.poll() is False
    assert handler.deviceList[0].references["value"].val is None


def test_partial_coil_group_reads_only_declared_bits_and_cannot_be_written():
    master = FakeModbusMaster(coils=[True] * 8)
    handler = handler_for(
        "device,dev,1\npoll,coil,0,4,BE_BE\nref,flags,0,bool8,rw\n", master
    )
    assert handler.poll()
    ref = handler.deviceList[0].references["flags"]
    assert ref.val == [True] * 4 + [False] * 4
    assert ref.quality == "partial"
    assert handler.read_references("dev", ["flags"])["flags"] == ref.val
    assert handler.write_reference("dev", "flags", [False] * 8) is False
    assert not master.writes


def test_batch_prevalidation_and_uncertain_transport_results():
    clear_threading_event()
    handler = handler_for(
        CONFIG
        + "ref,first,0,uint16,rw\nref,second,1,uint16,rw\nref,third,2,uint16,rw\n"
    )
    handler.write_references("dev", {"first": 1, "second": -1})
    assert not handler.modbus_client.writes
    assert handler.last_write_results["first"]["status"] == "not_attempted"
    assert handler.last_write_results["second"]["status"] == "rejected"
    handler.modbus_client.write_register = MagicMock(
        side_effect=[FakeModbusResult(), OSError("lost ACK")]
    )
    manager = ModbusConnectionManager(handler.modbus_client)
    result = manager.execute(
        "write",
        lambda: handler.write_references("dev", {"first": 1, "second": 2, "third": 3}),
    )
    assert not result.ok
    assert {
        name: entry["status"] for name, entry in handler.last_write_results.items()
    } == {"first": "confirmed", "second": "unknown", "third": "not_attempted"}
    assert handler.modbus_client.write_register.call_count == 2


def test_get_respects_write_only_permissions():
    handler = handler_for(CONFIG + "ref,secret,0,uint16,w\n")
    handler.modbus_client.read_holding_registers = MagicMock(
        side_effect=AssertionError("must not read")
    )
    assert handler.read_references("dev", ["secret"]) == {}


def test_recovery_probes_after_deadline_and_backoff_is_bounded(monkeypatch):
    handler = handler_for(CONFIG + "ref,value,0,uint16,r\n")
    handler.autoremove = True
    handler.recover_after = 5
    handler.modbus_client.fail_addresses = {0}
    monkeypatch.setattr("modpoll.modbus_task.time.monotonic", lambda: 10)
    for _ in range(3):
        assert not handler.poll()
    poller = handler.deviceList[0].pollerList[0]
    assert poller.disabled and poller.retry_at == 15
    handler.modbus_client.fail_addresses.clear()
    assert not handler.poll()
    monkeypatch.setattr("modpoll.modbus_task.time.monotonic", lambda: 16)
    assert handler.poll() and not poller.disabled
    manager = ModbusConnectionManager(handler.modbus_client)
    for index in range(2000):
        manager._record_failure("offline", float(index))
    assert manager.backoff_until == 2059.0
    assert manager.consecutive_failures == 2000


@pytest.mark.parametrize(
    "payload",
    [
        b"\xff",
        b"[]",
        b'{"x":1,"x":2}',
        b'{"x":NaN}',
        b'{"x":Infinity}',
        b'{"x":1e999}',
        b'{"x":{}}',
    ],
)
def test_strict_command_json(payload):
    with pytest.raises(ValueError):
        parse_command(payload, "set", 65536, 30)


def test_versioned_command_deduplication_and_expiry(monkeypatch):
    monkeypatch.setattr("modpoll.commands.time.time", lambda: 100)
    obj = {
        "version": 1,
        "request_id": "abc",
        "operation": "set",
        "values": {"value": 2},
        "expires_at": 101,
    }
    raw = json.dumps(obj).encode()
    assert parse_command(raw, "command", 65536, 30).request_id == "abc"
    cache = RequestCache(size=1)
    cache.save("dev", "abc", raw, "reply")
    assert cache.lookup("dev", "abc", raw) == "reply"
    with pytest.raises(ValueError, match="different payload"):
        cache.lookup("dev", "abc", raw + b" ")
    monkeypatch.setattr("modpoll.commands.time.time", lambda: 102)
    with pytest.raises(ValueError, match="expired"):
        parse_command(raw, "command", 65536, 30)


def test_dispatch_deduplicates_write_and_rejects_malformed_without_crashing():
    clear_threading_event()
    args = get_parser().parse_args(["--config", "dummy.csv", "--tcp", "localhost"])
    handler = handler_for(CONFIG + "ref,value,0,uint16,rw\n")
    mqtt = MagicMock()
    manager = ModbusConnectionManager(handler.modbus_client)
    cache = RequestCache()
    payload = json.dumps(
        {"version": 1, "request_id": "abc", "operation": "set", "values": {"value": 7}}
    ).encode()
    for _ in range(2):
        main._handle_command(
            args, mqtt, [handler], manager, cache, "modpoll/dev/commands", payload
        )
    assert len(handler.modbus_client.writes) == 1
    assert mqtt.publish_response.call_count == 2
    main._handle_command(
        args, mqtt, [handler], manager, cache, "modpoll/dev/commands", b"\xff"
    )
    assert json.loads(mqtt.publish_response.call_args.args[1])["status"] == "rejected"


def test_expiry_during_connection_prevents_write():
    clear_threading_event()
    handler = handler_for(CONFIG + "ref,value,0,uint16,rw\n")
    handler.command_expires_at = time.time() - 1
    handler.write_references("dev", {"value": 1})
    assert not handler.modbus_client.writes
    assert handler.last_write_results["value"]["error"] == "command_expired"


def test_mqtt_retained_byte_budget_age_and_nonretained_responses(monkeypatch):
    clear_threading_event()
    handler = MqttHandler(
        "test", "localhost", 1883, None, None, None, 1, rx_queue_size=1, queue_bytes=32
    )
    handler.mqtt_client = MagicMock()
    handler.mqtt_client.is_connected.return_value = True
    handler.mqtt_client.publish.return_value.rc = 0
    handler.mqtt_client.publish.return_value.is_published.return_value = True

    def message(payload, retain=False):
        return SimpleNamespace(topic="modpoll/dev/set", payload=payload, retain=retain)

    handler._on_message(handler.mqtt_client, None, message(b'{"value":1}', True))
    assert handler.receive() == (None, None)
    handler._on_message(handler.mqtt_client, None, message(b'{"value":1}'))
    handler._on_message(handler.mqtt_client, None, message(b'{"value":2}'))
    assert handler.dropped_messages == 2
    assert handler.receive() == ("modpoll/dev/set", b'{"value":1}')
    assert handler._queued_bytes == 0
    handler._on_message(handler.mqtt_client, None, message(b"x" * 33))
    assert handler.dropped_messages == 3
    handler._on_message(handler.mqtt_client, None, message(b'{"value":1}'))
    old = time.monotonic()
    monkeypatch.setattr("modpoll.mqtt_task.time.monotonic", lambda: old + 31)
    assert handler.receive() == (None, None)
    assert handler.expired_messages == 1
    handler.retain_data_publishes = True
    handler.publish_response("reply", "{}")
    assert handler.mqtt_client.publish.call_args.args[3] is False


def test_atomic_export_aggregates_and_preserves_previous_file_on_failure(
    tmp_path, monkeypatch
):
    one = handler_for(CONFIG + "ref,value,0,uint16,r\n").deviceList[0]
    two = handler_for(
        CONFIG.replace("device,dev,", "device,other,") + "ref,value,0,uint16,r\n"
    ).deviceList[0]
    one.references["value"].update_value(3)
    path = tmp_path / "snapshot.json"
    export_snapshot(str(path), [one, two], 100)
    payload = json.loads(path.read_text())
    assert payload == {
        "dev": {"value": 3, "timestamp": 100},
        "other": {"value": None, "timestamp": 100},
    }
    previous = path.read_bytes()
    monkeypatch.setattr(
        "modpoll.export.os.replace", MagicMock(side_effect=OSError("disk failure"))
    )
    with pytest.raises(OSError):
        export_snapshot(str(path), [one])
    assert path.read_bytes() == previous
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize(
    "option,value",
    [
        ("--rate", "nan"),
        ("--timeout", "inf"),
        ("--tcp-port", "0"),
        ("--mqtt-port", "65536"),
        ("--mqtt-command-ttl", "0"),
        ("--mqtt-queue-bytes", "0"),
    ],
)
def test_invalid_cli_rejected(option, value):
    with pytest.raises(SystemExit) as error:
        get_parser().parse_args(
            ["--config", "dummy.csv", "--tcp", "localhost", option, value]
        )
    assert error.value.code == 2


def test_offline_validation_and_cross_file_duplicates(tmp_path, monkeypatch):
    config = tmp_path / "config.csv"
    config.write_text(CONFIG + "ref,value,0,uint16,r\n")
    args = get_parser().parse_args(["--check-config", "--config", str(config)])
    monkeypatch.setattr(
        "modpoll.modbus_task._create_modbus_client",
        MagicMock(side_effect=AssertionError("No transport")),
    )
    _, handlers = setup_modbus_handlers(args)
    assert len(handlers) == 1
    args.config.append(str(config))
    with pytest.raises(ConfigurationError, match="Duplicate device"):
        setup_modbus_handlers(args)
    assert (
        safe_source("https://user:secret@example.com/config.csv?token=secret")
        == "https://example.com/config.csv"
    )


def test_once_returns_failure_and_always_closes_transport(monkeypatch):
    clear_threading_event()
    master = FakeModbusMaster(registers=[0] * 8, fail_addresses={0})
    handler = handler_for(CONFIG + "ref,value,0,uint16,r\n", master)
    monkeypatch.setattr(main, "setup_modbus_handlers", lambda args: (master, [handler]))
    monkeypatch.setattr(
        sys,
        "argv",
        ["modpoll", "--config", "dummy.csv", "--tcp", "localhost", "--once"],
    )
    with pytest.raises(SystemExit) as error:
        main.app()
    assert error.value.code == 1
    assert master.close_count == 1


@pytest.mark.parametrize(
    "source",
    sorted(
        [
            path
            for directory in ("examples", "contrib")
            for path in (Path(__file__).resolve().parents[1] / directory).rglob("*")
            if path.suffix in (".csv", ".cfg")
        ]
    ),
)
def test_shipped_configurations_validate(source):
    assert read_config(str(source), ",", 3, logging.getLogger(__name__))


def test_nonfinite_get_does_not_claim_confirmed_null():
    handler = handler_for(
        CONFIG + "ref,value,0,float32,r\n",
        FakeModbusMaster(registers=[0x7F80, 0] + [0] * 6),
    )
    assert handler.read_references("dev", ["value"]) == {}
    assert handler.deviceList[0].getReadErrors == 1


def test_request_cache_evicts_by_bytes_and_expiry(monkeypatch):
    now = [1.0]
    monkeypatch.setattr("modpoll.commands.time.monotonic", lambda: now[0])
    cache = RequestCache(size=100, ttl=5, max_bytes=5)
    cache.save("dev", "one", b"one", "123")
    cache.save("dev", "two", b"two", "456")
    assert cache.byte_count == 3 and len(cache.entries) == 1
    assert cache.lookup("dev", "one", b"one") is None
    now[0] = 10
    assert cache.lookup("dev", "two", b"two") is None
    assert cache.byte_count == 0


def test_read_only_dispatch_never_connects_to_write():
    clear_threading_event()
    args = get_parser().parse_args(
        ["--config", "dummy.csv", "--tcp", "localhost", "--read-only"]
    )
    handler = handler_for(CONFIG + "ref,value,0,uint16,rw\n")
    mqtt = MagicMock()
    manager = ModbusConnectionManager(handler.modbus_client)
    raw = json.dumps(
        {
            "version": 1,
            "request_id": "readonly",
            "operation": "set",
            "values": {"value": 7},
        }
    ).encode()
    main._handle_command(
        args, mqtt, [handler], manager, RequestCache(), "modpoll/dev/commands", raw
    )
    assert handler.modbus_client.connect_count == 0 and not handler.modbus_client.writes
    reply = json.loads(mqtt.publish_response.call_args.args[1])
    assert reply["results"]["value"]["error"] == "read_only"


def test_ambiguous_command_topics_rejected():
    with pytest.raises(SystemExit) as error:
        get_parser().parse_args(
            [
                "--config",
                "dummy.csv",
                "--tcp",
                "localhost",
                "--mqtt-get-topic-pattern",
                "modpoll/get/+",
                "--mqtt-subscribe-topic-pattern",
                "modpoll/+/get",
            ]
        )
    assert error.value.code == 2


def test_invalid_transport_selection_is_configuration_error(tmp_path):
    config = tmp_path / "config.csv"
    config.write_text(CONFIG + "ref,value,0,uint16,r\n")
    args = get_parser().parse_args(["--config", str(config)])
    with pytest.raises(ConfigurationError, match="communication method"):
        setup_modbus_handlers(args)


def test_wall_clock_jump_preserves_acquisition_time_and_monotonic_backoff(monkeypatch):
    utc = [1000.0]
    mono = [10.0]
    monkeypatch.setattr("modpoll.modbus_connection.get_utc_time", lambda: utc[0])
    manager = ModbusConnectionManager(FakeModbusMaster(), clock=lambda: mono[0])
    assert manager.ensure_connected()
    assert manager.diagnostics()["modbus_last_success_at"] == 1000.0
    utc[0] = 500.0
    mono[0] = 20.0
    assert manager.diagnostics()["modbus_last_success_at"] == 1000.0
    manager._record_failure("offline", mono[0])
    assert manager.backoff_until == 21.0
    utc[0] = 50000.0
    assert manager.ensure_connected() is False


def test_invalid_url_is_a_sanitized_configuration_error():
    from modpoll.configuration import load_text

    with pytest.raises(ConfigurationError, match="invalid source"):
        load_text("http://[", 3)
