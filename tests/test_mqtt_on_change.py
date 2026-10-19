"""Behavior of --mqtt-on-change: publish data only when values change."""

import json
from unittest.mock import MagicMock

from modpoll.arg_parser import get_parser
from modpoll.modbus_models import Device, Reference
from modpoll.modbus_task import ModbusHandler


def _device(*refs: tuple[str, str, object]) -> Device:
    device = Device("dev", 1)
    device.pollSuccess = True
    mapping = {}
    for name, dtype, val in refs:
        ref = Reference(device, name, "0", dtype, "r", None, None)
        ref.val = val
        mapping[name] = ref
    device.references = mapping
    return device


def _mqtt() -> MagicMock:
    mqtt = MagicMock()
    mqtt.connection_generation = 0
    mqtt.publish_errors = 0
    return mqtt


def _handler(
    mqtt: MagicMock, *, single_publish: bool = False, on_change: bool = True
) -> ModbusHandler:
    return ModbusHandler(
        MagicMock(),
        "dummy.csv",
        mqtt_handler=mqtt,
        mqtt_publish_topic_pattern="modpoll/{{device_name}}/data",
        mqtt_single_publish=single_publish,
        mqtt_keys="name-only",
        mqtt_on_change=on_change,
    )


def _published(mqtt: MagicMock) -> list[tuple[str, str]]:
    return [call[0] for call in mqtt.publish_data_message.call_args_list]


def test_mqtt_on_change_cli_option():
    args = get_parser().parse_args(
        ["--config", "dummy.csv", "--tcp", "127.0.0.1", "--mqtt-on-change"]
    )
    assert args.mqtt_on_change is True

    defaults = get_parser().parse_args(["--config", "dummy.csv", "--tcp", "127.0.0.1"])
    assert defaults.mqtt_on_change is False


def test_on_change_publishes_first_payload_then_skips_unchanged():
    mqtt = _mqtt()
    handler = _handler(mqtt)
    handler.deviceList = [_device(("temp", "uint16", 21))]

    handler.publish_data()
    handler.publish_data()

    published = _published(mqtt)
    assert len(published) == 1
    topic, msg = published[0]
    assert topic == "modpoll/dev/data"
    assert json.loads(msg) == {"temp": 21}


def test_on_change_publishes_changed_value():
    mqtt = _mqtt()
    handler = _handler(mqtt)
    device = _device(("temp", "uint16", 21))
    handler.deviceList = [device]

    handler.publish_data()
    device.references["temp"].val = 22
    handler.publish_data()

    published = _published(mqtt)
    assert len(published) == 2
    assert json.loads(published[1][1]) == {"temp": 22}


def test_on_change_ignores_timestamp_only_changes():
    mqtt = _mqtt()
    handler = _handler(mqtt)
    handler.deviceList = [_device(("temp", "uint16", 21))]

    handler.publish_data(timestamp=100.0)
    handler.publish_data(timestamp=200.0)

    published = _published(mqtt)
    assert len(published) == 1
    assert json.loads(published[0][1]) == {"temp": 21, "timestamp": 100.0}


def test_on_change_compares_mqtt_rounded_floats():
    mqtt = _mqtt()
    handler = _handler(mqtt)
    device = _device(("temp", "float32", 1.0001))
    handler.deviceList = [device]

    handler.publish_data()
    device.references["temp"].val = 1.0002
    handler.publish_data()

    assert len(_published(mqtt)) == 1


def test_on_change_single_keeps_colliding_mqtt_keys_independent():
    device = Device("dev", 1)
    device.pollSuccess = True
    first = Reference(device, "a|b", "0", "uint16", "r", None, None)
    first.val = 1
    second = Reference(device, "a", "1", "uint16", "r", "b", None)
    second.val = 1
    device.references = {"a|b": first, "a": second}

    mqtt = _mqtt()
    handler = ModbusHandler(
        MagicMock(),
        "dummy.csv",
        mqtt_handler=mqtt,
        mqtt_publish_topic_pattern="modpoll/{{device_name}}/data",
        mqtt_single_publish=True,
        mqtt_keys="name-with-unit",
        mqtt_on_change=True,
    )
    handler.deviceList = [device]

    handler.publish_data()
    assert _published(mqtt) == [
        ("modpoll/dev/data/a|b", "1"),
        ("modpoll/dev/data/a", "1"),
    ]

    # Changing the second reference must not be hidden by the first one's
    # cached value: the two topics have distinct cache entries.
    second.val = 2
    handler.publish_data()
    assert _published(mqtt)[-1] == ("modpoll/dev/data/a", "2")

    handler.publish_data()
    assert len(_published(mqtt)) == 3


def test_on_change_disabled_publishes_every_cycle():
    mqtt = _mqtt()
    handler = _handler(mqtt, on_change=False)
    handler.deviceList = [_device(("temp", "uint16", 21))]

    handler.publish_data()
    handler.publish_data()

    assert len(_published(mqtt)) == 2


def test_on_change_single_publishes_only_changed_references():
    mqtt = _mqtt()
    handler = _handler(mqtt, single_publish=True)
    device = _device(("temp", "uint16", 21), ("hum", "uint16", 55))
    handler.deviceList = [device]

    handler.publish_data()
    device.references["temp"].val = 22
    handler.publish_data()

    published = _published(mqtt)
    assert published == [
        ("modpoll/dev/data/temp", "21"),
        ("modpoll/dev/data/hum", "55"),
        ("modpoll/dev/data/temp", "22"),
    ]


def test_on_change_reconnect_invalidates_cache():
    mqtt = _mqtt()
    handler = _handler(mqtt)
    handler.deviceList = [_device(("temp", "uint16", 21))]

    handler.publish_data()
    handler.publish_data()
    assert len(_published(mqtt)) == 1

    mqtt.connection_generation += 1
    handler.publish_data()

    assert len(_published(mqtt)) == 2


def test_on_change_publish_error_invalidates_cache():
    mqtt = _mqtt()
    handler = _handler(mqtt)
    handler.deviceList = [_device(("temp", "uint16", 21))]

    handler.publish_data()
    handler.publish_data()
    assert len(_published(mqtt)) == 1

    # A broker rejection is reported asynchronously after the publish returns.
    mqtt.publish_errors += 1
    handler.publish_data()

    assert len(_published(mqtt)) == 2


def test_on_change_failed_publish_is_retried():
    mqtt = _mqtt()
    handler = _handler(mqtt)
    handler.deviceList = [_device(("temp", "uint16", 21))]

    mqtt.publish_data_message.return_value = None
    handler.publish_data()
    attempts_after_failure = len(_published(mqtt))
    mqtt.publish_data_message.return_value = MagicMock()
    handler.publish_data()

    assert len(_published(mqtt)) == attempts_after_failure + 1


def test_on_change_device_recovery_republishes_unchanged_values():
    mqtt = _mqtt()
    handler = _handler(mqtt)
    device = _device(("temp", "uint16", 21))
    handler.deviceList = [device]

    handler.publish_data()
    device.pollSuccess = False
    handler.publish_data()
    assert len(_published(mqtt)) == 1

    device.pollSuccess = True
    handler.publish_data()

    assert len(_published(mqtt)) == 2


def test_on_change_copies_list_values():
    mqtt = _mqtt()
    handler = _handler(mqtt)
    device = _device(("flags", "bool8", [True, False]))
    handler.deviceList = [device]

    handler.publish_data()
    device.references["flags"].val[0] = False
    handler.publish_data()

    assert len(_published(mqtt)) == 2
