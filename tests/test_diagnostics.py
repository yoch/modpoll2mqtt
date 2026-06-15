import json
from unittest.mock import MagicMock

from modpoll.modbus_task import (
    Device,
    ModbusHandler,
    count_devices_failing,
    publish_global_diagnostics,
    MQTT_GLOBAL_DIAGNOSTICS_TOPIC,
)


def _handler_with_devices(*devices_failing: bool) -> ModbusHandler:
    handler = ModbusHandler(MagicMock(), "a.csv")
    handler.deviceList = []
    for i, failing in enumerate(devices_failing):
        dev = Device(f"dev{i}", 1)
        dev.pollSuccess = not failing
        handler.deviceList.append(dev)
    return handler


def test_count_devices_failing_across_handlers():
    handlers = [
        _handler_with_devices(False, True),
        _handler_with_devices(True, True),
    ]
    assert count_devices_failing(handlers) == 3


def test_publish_global_diagnostics():
    mqtt = MagicMock()
    mqtt.is_connected.return_value = True
    mqtt.retain_data_publishes = False
    handlers = [_handler_with_devices(False, True)]

    publish_global_diagnostics(mqtt, handlers, modbus_ok=True, last_cycle_s=9.8)

    mqtt.publish.assert_called_once()
    topic, body = mqtt.publish.call_args[0]
    assert topic == MQTT_GLOBAL_DIAGNOSTICS_TOPIC
    assert json.loads(body) == {
        "mqtt_connected": True,
        "modbus_ok": True,
        "devices_failing": 1,
        "last_cycle_s": 9.8,
    }
    assert mqtt.publish.call_args.kwargs["retain"] is False


def test_publish_global_diagnostics_when_disconnected():
    mqtt = MagicMock()
    mqtt.is_connected.return_value = False
    mqtt.retain_data_publishes = False
    handlers = [_handler_with_devices()]

    publish_global_diagnostics(mqtt, handlers, modbus_ok=True, last_cycle_s=5.0)

    assert json.loads(mqtt.publish.call_args[0][1])["mqtt_connected"] is False


def test_publish_global_diagnostics_respects_mqtt_retain():
    mqtt = MagicMock()
    mqtt.is_connected.return_value = True
    mqtt.retain_data_publishes = True
    handlers = [_handler_with_devices()]

    publish_global_diagnostics(mqtt, handlers, modbus_ok=False, last_cycle_s=1.0)

    assert mqtt.publish.call_args.kwargs["retain"] is True
