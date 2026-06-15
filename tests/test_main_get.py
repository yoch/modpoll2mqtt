import json
import sys
from unittest.mock import MagicMock

import pytest

from modpoll import main
from modpoll.modbus_task import Device, ModbusHandler, Poller, Reference
from modpoll.utils import clear_threading_event
from tests.helpers.modbus import FakeModbusMaster


@pytest.fixture(autouse=True)
def _reset_loop_exit_event():
    clear_threading_event()
    yield
    clear_threading_event()


def test_main_loop_publishes_get_response(monkeypatch):
    device = Device("dev", 1)
    poller = Poller(device, 3, 0, 1, "BE_BE")
    ref = Reference(device, "temp", "0", "uint16", "r", None, None)
    poller.add_readable_reference(ref)
    device.pollerList = [poller]
    device.references = {"temp": ref}

    master = FakeModbusMaster(registers=[42])
    handler = ModbusHandler(master, "dummy.csv")
    handler.deviceList = [device]

    published = []

    class FakeMqttHandler:
        def __init__(self, *args, **kwargs):
            pass

        def setup(self):
            return True

        def connect(self):
            return True

        def close(self):
            pass

        def receive(self):
            if not hasattr(self, "_sent"):
                self._sent = True
                return "modpoll/dev/get", b'{"temp": null}'
            main.set_threading_event()
            return None, None

        def publish_data_message(self, topic, msg):
            published.append((topic, msg))

    monkeypatch.setattr(main, "MqttHandler", FakeMqttHandler)
    monkeypatch.setattr(
        main,
        "setup_modbus_handlers",
        lambda args, mqtt_handler: (master, [handler]),
    )
    monkeypatch.setattr(main, "modbus_connect", lambda client: True)
    monkeypatch.setattr(main, "modbus_close", lambda client: None)
    monkeypatch.setattr(main, "delay_thread", lambda *a, **k: None)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "modpoll",
            "--config",
            "dummy.csv",
            "--tcp",
            "127.0.0.1",
            "--mqtt-host",
            "localhost",
            "--rate",
            "3600",
        ],
    )

    main.app()

    assert len(published) == 1
    topic, payload = published[0]
    assert topic == "modpoll/dev/get/response"
    assert json.loads(payload) == {"temp": 42}
    assert device.getCount == 1
    assert device.getErrors == 0
    assert device.getSuccess == 1


def test_main_loop_get_failure_publishes_empty_response(monkeypatch):
    device = Device("dev", 1)
    device.pollerList = []
    device.references = {}

    handler = ModbusHandler(MagicMock(), "dummy.csv")
    handler.deviceList = [device]

    published = []

    class FakeMqttHandler:
        def __init__(self, *args, **kwargs):
            pass

        def setup(self):
            return True

        def connect(self):
            return True

        def close(self):
            pass

        def receive(self):
            if not hasattr(self, "_sent"):
                self._sent = True
                return "modpoll/dev/get", b'{"missing": null}'
            main.set_threading_event()
            return None, None

        def publish_data_message(self, topic, msg):
            published.append((topic, msg))

    monkeypatch.setattr(main, "MqttHandler", FakeMqttHandler)
    monkeypatch.setattr(
        main,
        "setup_modbus_handlers",
        lambda args, mqtt_handler: (MagicMock(), [handler]),
    )
    monkeypatch.setattr(main, "modbus_connect", lambda client: True)
    monkeypatch.setattr(main, "modbus_close", lambda client: None)
    monkeypatch.setattr(main, "delay_thread", lambda *a, **k: None)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "modpoll",
            "--config",
            "dummy.csv",
            "--tcp",
            "127.0.0.1",
            "--mqtt-host",
            "localhost",
            "--rate",
            "3600",
        ],
    )

    main.app()

    assert len(published) == 1
    topic, payload = published[0]
    assert topic == "modpoll/dev/get/response"
    assert json.loads(payload) == {}
    assert device.getCount == 1
    assert device.getErrors == 1
    assert device.getUnknownRefs == 1
