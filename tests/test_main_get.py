import json
import sys
from unittest.mock import MagicMock

import pytest
from pymodbus.exceptions import ModbusException
from tests.helpers.modbus import FakeModbusMaster

from modpoll import main
from modpoll.modbus_task import Device, ModbusHandler, Poller, Reference
from modpoll.utils import clear_threading_event


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
    assert master.connect_count == 1
    assert master.close_count == 1


def test_main_loop_get_failure_publishes_empty_response(monkeypatch):
    device = Device("dev", 1)
    device.pollerList = []
    device.references = {}

    master = FakeModbusMaster()
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
                return "modpoll/dev/get", b'{"missing": null}'
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
    assert master.connect_count == 1
    assert master.close_count == 1


class _FakeMqttScript:
    published = []
    published_raw = []
    closed = False
    messages = []

    def __init__(self, *args, **kwargs):
        self._index = 0

    def setup(self):
        return True

    def connect(self):
        return True

    def close(self):
        type(self).closed = True

    def is_connected(self):
        return True

    @property
    def retain_data_publishes(self):
        return False

    def receive(self):
        if self._index < len(type(self).messages):
            message = type(self).messages[self._index]
            self._index += 1
            return message
        main.set_threading_event()
        return None, None

    def publish_data_message(self, topic, msg):
        type(self).published.append((topic, msg))

    def publish(self, topic, msg, retain=False):
        type(self).published_raw.append((topic, msg, retain))


class _ExplodingReadMaster(FakeModbusMaster):
    def read_holding_registers(self, address, *, count=1, device_id=1):
        raise OSError("wire cut")


def _device_with_temp_ref():
    device = Device("dev", 1)
    poller = Poller(device, 3, 0, 1, "BE_BE")
    ref = Reference(device, "temp", "0", "uint16", "r", None, None)
    poller.add_readable_reference(ref)
    device.pollerList = [poller]
    device.references = {"temp": ref}
    return device


def _run_main_with_script(monkeypatch, master, handler, messages, extra_args=None):
    _FakeMqttScript.published = []
    _FakeMqttScript.published_raw = []
    _FakeMqttScript.closed = False
    _FakeMqttScript.messages = list(messages)

    monkeypatch.setattr(main, "MqttHandler", _FakeMqttScript)
    monkeypatch.setattr(
        main,
        "setup_modbus_handlers",
        lambda args, mqtt_handler: (master, [handler]),
    )
    monkeypatch.setattr(main, "delay_thread", lambda *a, **k: None)
    argv = [
        "modpoll",
        "--config",
        "dummy.csv",
        "--tcp",
        "127.0.0.1",
        "--mqtt-host",
        "localhost",
        "--rate",
        "3600",
    ]
    if extra_args:
        argv.extend(extra_args)
    monkeypatch.setattr(sys, "argv", argv)

    main.app()
    return _FakeMqttScript


def test_main_poll_transport_failure_closes_and_get_is_fast_failed_by_backoff(
    monkeypatch,
):
    device = _device_with_temp_ref()
    master = _ExplodingReadMaster(registers=[42])
    handler = ModbusHandler(master, "dummy.csv")
    handler.deviceList = [device]

    mqtt = _run_main_with_script(
        monkeypatch,
        master,
        handler,
        [("modpoll/dev/get", b'{"temp": null}')],
    )

    assert json.loads(mqtt.published[0][1]) == {}
    assert master.connect_count == 1
    assert master.close_count == 2
    assert device.pollSuccess is False
    assert device.errorCount == 1
    assert device.getCount == 1
    assert device.getErrors == 1


def test_main_diagnostics_expose_backoff_after_transport_failure(monkeypatch):
    device = _device_with_temp_ref()
    master = _ExplodingReadMaster(registers=[42])
    handler = ModbusHandler(
        master,
        "dummy.csv",
        mqtt_diagnostics_topic_pattern="modpoll/{{device_name}}/diagnostics",
    )
    handler.deviceList = [device]

    mqtt = _run_main_with_script(
        monkeypatch,
        master,
        handler,
        [],
        extra_args=["--diagnostics-rate", "1"],
    )

    global_diagnostics = [
        json.loads(payload)
        for topic, payload, _retain in mqtt.published_raw
        if topic == "modpoll/diagnostics"
    ]
    assert global_diagnostics
    payload = global_diagnostics[-1]
    assert payload["modbus_ok"] is False
    assert payload["modbus_connection_state"] == "BACKOFF"
    assert payload["modbus_connected"] is False
    assert payload["modbus_consecutive_failures"] == 1
    assert payload["modbus_transaction_failure_count"] == 1
    assert "wire cut" in payload["modbus_last_error"]
    assert payload["modbus_backoff_until"] is not None


def test_main_connect_failure_marks_all_handlers_without_callback(monkeypatch):
    device = _device_with_temp_ref()
    master = FakeModbusMaster(registers=[42])
    master.connect = MagicMock(return_value=False)
    handler = ModbusHandler(master, "dummy.csv")
    handler.deviceList = [device]

    mqtt = _run_main_with_script(monkeypatch, master, handler, [])

    assert mqtt.published == []
    assert master.connect.call_count == 1
    assert master.close_count == 1
    assert device.pollSuccess is False
    assert device.errorCount == 1
    assert device.pollCount == 1


def test_main_write_reuses_connection_after_poll(monkeypatch):
    device = Device("dev", 1)
    poller = Poller(device, 3, 0, 1, "BE_BE")
    ref = Reference(device, "target", "0", "uint16", "rw", None, None)
    poller.add_readable_reference(ref)
    device.pollerList = [poller]
    device.references = {"target": ref}
    master = FakeModbusMaster(registers=[7])
    handler = ModbusHandler(master, "dummy.csv")
    handler.deviceList = [device]

    _run_main_with_script(
        monkeypatch,
        master,
        handler,
        [("modpoll/dev/set", b'{"target": 9}')],
    )

    assert master.connect_count == 1
    assert master.close_count == 1
    assert master.writes == [("register", 0, 9)]
    assert device.setCount == 1
    assert device.setErrors == 0
    assert device.setSuccess == 1


class _ReadFailsAfterPollMaster(FakeModbusMaster):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._holding_reads = 0

    def read_holding_registers(self, address, *, count=1, device_id=1):
        self._holding_reads += 1
        if self._holding_reads == 1:
            return super().read_holding_registers(
                address, count=count, device_id=device_id
            )
        raise OSError("wire cut during get")


class _WriteFailsAfterPollMaster(FakeModbusMaster):
    def write_register(self, address, value, device_id=1):
        raise OSError("wire cut during set")


def test_main_mid_get_transport_failure_does_not_double_count_attempt(monkeypatch):
    device = _device_with_temp_ref()
    master = _ReadFailsAfterPollMaster(registers=[42])
    handler = ModbusHandler(master, "dummy.csv")
    handler.deviceList = [device]

    mqtt = _run_main_with_script(
        monkeypatch,
        master,
        handler,
        [("modpoll/dev/get", b'{"temp": null}')],
    )

    assert json.loads(mqtt.published[0][1]) == {}
    assert device.getCount == 1
    assert device.getErrors == 1
    assert device.getReadErrors == 1
    assert device.getSuccess == 0
    assert master.connect_count == 1
    assert master.close_count == 2


def test_main_mid_set_transport_failure_does_not_double_count_attempt(monkeypatch):
    device = Device("dev", 1)
    poller = Poller(device, 3, 0, 1, "BE_BE")
    ref = Reference(device, "target", "0", "uint16", "rw", None, None)
    poller.add_readable_reference(ref)
    device.pollerList = [poller]
    device.references = {"target": ref}
    master = _WriteFailsAfterPollMaster(registers=[7])
    handler = ModbusHandler(master, "dummy.csv")
    handler.deviceList = [device]

    _run_main_with_script(
        monkeypatch,
        master,
        handler,
        [("modpoll/dev/set", b'{"target": 9}')],
    )

    assert device.setCount == 1
    assert device.setErrors == 1
    assert device.setSuccess == 0
    assert master.connect_count == 1
    assert master.close_count == 2


class _SecondPollFailsMaster(FakeModbusMaster):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._holding_reads = 0

    def read_holding_registers(self, address, *, count=1, device_id=1):
        self._holding_reads += 1
        if self._holding_reads == 1:
            return super().read_holding_registers(
                address, count=count, device_id=device_id
            )
        raise OSError("wire cut on second handler")


def _handler_for_device_name(master, device_name):
    device = Device(device_name, 1)
    poller = Poller(device, 3, 0, 1, "BE_BE")
    ref = Reference(device, "temp", "0", "uint16", "r", None, None)
    poller.add_readable_reference(ref)
    device.pollerList = [poller]
    device.references = {"temp": ref}
    handler = ModbusHandler(master, f"{device_name}.csv")
    handler.deviceList = [device]
    return handler


def test_global_modbus_ok_false_when_later_handler_hits_transport_error(monkeypatch):
    master = _SecondPollFailsMaster(registers=[42])
    ok_handler = _handler_for_device_name(master, "ok")
    failing_handler = _handler_for_device_name(master, "failing")

    _FakeMqttScript.published = []
    _FakeMqttScript.published_raw = []
    _FakeMqttScript.closed = False
    _FakeMqttScript.messages = []

    monkeypatch.setattr(main, "MqttHandler", _FakeMqttScript)
    monkeypatch.setattr(
        main,
        "setup_modbus_handlers",
        lambda args, mqtt_handler: (master, [ok_handler, failing_handler]),
    )
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
            "--diagnostics-rate",
            "1",
        ],
    )

    main.app()

    global_diagnostics = [
        json.loads(payload)
        for topic, payload, _retain in _FakeMqttScript.published_raw
        if topic == "modpoll/diagnostics"
    ]
    assert global_diagnostics
    assert global_diagnostics[-1]["modbus_ok"] is False
    assert global_diagnostics[-1]["modbus_transaction_failure_count"] == 1
    assert ok_handler.deviceList[0].pollSuccess is True
    assert failing_handler.deviceList[0].pollSuccess is False


class _ReadRaisesModbusExceptionAfterPollMaster(FakeModbusMaster):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._holding_reads = 0

    def read_holding_registers(self, address, *, count=1, device_id=1):
        self._holding_reads += 1
        if self._holding_reads == 1:
            return super().read_holding_registers(
                address, count=count, device_id=device_id
            )
        raise ModbusException("modbus get transport failure")


class _WriteRaisesModbusExceptionAfterPollMaster(FakeModbusMaster):
    def write_register(self, address, value, device_id=1):
        raise ModbusException("modbus set transport failure")


def test_main_mid_get_modbus_exception_closes_and_backs_off(monkeypatch):
    device = _device_with_temp_ref()
    master = _ReadRaisesModbusExceptionAfterPollMaster(registers=[42])
    handler = ModbusHandler(master, "dummy.csv")
    handler.deviceList = [device]

    mqtt = _run_main_with_script(
        monkeypatch,
        master,
        handler,
        [("modpoll/dev/get", b'{"temp": null}')],
    )

    assert json.loads(mqtt.published[0][1]) == {}
    assert device.getCount == 1
    assert device.getErrors == 1
    assert device.getReadErrors == 1
    assert master.connect_count == 1
    assert master.close_count == 2


def test_main_mid_set_modbus_exception_closes_and_backs_off(monkeypatch):
    device = Device("dev", 1)
    poller = Poller(device, 3, 0, 1, "BE_BE")
    ref = Reference(device, "target", "0", "uint16", "rw", None, None)
    poller.add_readable_reference(ref)
    device.pollerList = [poller]
    device.references = {"target": ref}
    master = _WriteRaisesModbusExceptionAfterPollMaster(registers=[7])
    handler = ModbusHandler(master, "dummy.csv")
    handler.deviceList = [device]

    _run_main_with_script(
        monkeypatch,
        master,
        handler,
        [("modpoll/dev/set", b'{"target": 9}')],
    )

    assert device.setCount == 1
    assert device.setErrors == 1
    assert device.setSuccess == 0
    assert master.connect_count == 1
    assert master.close_count == 2
