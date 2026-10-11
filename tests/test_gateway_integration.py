"""Real CLI, local Mosquitto and isolated Modbus devices in one process-level test."""

import json
import queue
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path
from threading import Event

import pytest
from paho.mqtt.client import Client
from paho.mqtt.enums import CallbackAPIVersion, MQTTProtocolVersion
from pymodbus.client import ModbusTcpClient

from modpoll.mqtt_task import MqttHandler
from modpoll.utils import clear_threading_event

pytestmark = [pytest.mark.integration, pytest.mark.mqtt, pytest.mark.modbus]


class Collector:
    def __init__(self, host, port, prefix, version="5.0"):
        self.prefix = prefix
        self.messages = queue.Queue()
        self.subscribed = Event()
        self.client = Client(
            CallbackAPIVersion.VERSION2,
            protocol=MQTTProtocolVersion.MQTTv5
            if version == "5.0"
            else MQTTProtocolVersion.MQTTv311,
        )
        self.client.on_connect = lambda client, *_: client.subscribe(
            prefix + "/#", qos=1
        )
        self.client.on_subscribe = lambda *_: self.subscribed.set()
        self.client.on_message = lambda client, userdata, message: self.messages.put(
            (message.topic, message.payload, message.retain)
        )
        self.client.connect(host, port)
        self.client.loop_start()
        assert self.subscribed.wait(5)

    def publish(self, suffix, payload, retain=False):
        info = self.client.publish(self.prefix + suffix, payload, qos=1, retain=retain)
        info.wait_for_publish(timeout=5)
        assert info.is_published()

    def wait(self, suffix, predicate=lambda payload: True, timeout=8):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                topic, raw, retained = self.messages.get(
                    timeout=max(0.01, deadline - time.monotonic())
                )
            except queue.Empty:
                break
            if topic == self.prefix + suffix:
                payload = json.loads(raw)
                if predicate(payload):
                    return payload, retained
        pytest.fail(f"No expected message on {suffix}")

    def close(self):
        self.client.disconnect()
        self.client.loop_stop()


@pytest.mark.parametrize("version", ["5.0", "3.1.1"])
def test_cli_command_export_retained_dedup_and_shutdown(
    mqtt_broker, modbus_test_endpoint, tmp_path, version
):
    clear_threading_event()
    prefix = "gateway-test/" + uuid.uuid4().hex
    host, port = mqtt_broker
    observer = Collector(host, port, prefix, version)
    modbus = ModbusTcpClient(
        modbus_test_endpoint.host, port=modbus_test_endpoint.port, retries=0
    )
    assert modbus.connect()
    assert not modbus.write_register(60, 0, device_id=1).isError()
    assert not modbus.write_register(60, 23, device_id=2).isError()
    configs = []
    for device, identifier in (("one", 1), ("two", 2)):
        config = tmp_path / f"{device}.csv"
        config.write_text(
            f"device,{device},{identifier}\npoll,holding_register,60,1,BE_BE\nref,value,60,uint16,rw\n"
        )
        configs.append(str(config))
    snapshot = tmp_path / "snapshot.json"
    # A retained write left by an earlier client must never run on startup.
    observer.publish("/one/set", '{"value": 99}', retain=True)
    logfile = (tmp_path / "gateway.log").open("w")
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "from modpoll.main import app; app()",
            "--config",
            *configs,
            "--tcp",
            modbus_test_endpoint.host,
            "--tcp-port",
            str(modbus_test_endpoint.port),
            "--mqtt-host",
            host,
            "--mqtt-port",
            str(port),
            "--mqtt-version",
            version,
            "--mqtt-qos",
            "1",
            "--mqtt-namespace",
            prefix,
            "--rate",
            "0.1",
            "--export",
            str(snapshot),
            "--timestamp",
            "--no-output",
        ],
        cwd=Path(__file__).resolve().parents[1],
        stdout=logfile,
        stderr=subprocess.STDOUT,
    )
    try:
        observer.wait("/status", lambda p: p.get("online") is True)
        observer.wait("/one/data", lambda p: p.get("value") == 0)
        assert modbus.read_holding_registers(60, count=1, device_id=1).registers == [0]
        if version == "5.0":
            # Retain As Published exposes retained live commands, not only replay.
            observer.publish("/one/set", '{"value": 99}', retain=True)
            observer.publish("/one/get", '{"value": null}')
            live_retained, _ = observer.wait("/one/get/response")
            assert live_retained == {"value": 0}
        observer.publish("/one/set", b"\xff")
        command = json.dumps(
            {
                "version": 1,
                "request_id": "write-1",
                "operation": "set",
                "values": {"value": 7},
            }
        )
        observer.publish("/one/commands", command)
        first, retained = observer.wait(
            "/one/commands/response", lambda p: p.get("request_id") == "write-1"
        )
        assert first["results"]["value"]["status"] == "confirmed" and not retained
        assert not modbus.write_register(60, 9, device_id=1).isError()
        observer.publish("/one/commands", command)
        duplicate, _ = observer.wait(
            "/one/commands/response", lambda p: p.get("request_id") == "write-1"
        )
        assert duplicate == first
        assert modbus.read_holding_registers(60, count=1, device_id=1).registers == [9]
        observer.publish(
            "/one/commands",
            json.dumps(
                {
                    "version": 1,
                    "request_id": "get-1",
                    "operation": "get",
                    "references": ["value"],
                }
            ),
        )
        response, retained = observer.wait(
            "/one/commands/response", lambda p: p.get("request_id") == "get-1"
        )
        assert response["results"]["value"] == {"status": "confirmed", "value": 9}
        assert not retained
        observer.publish(
            "/one/commands",
            json.dumps(
                {
                    "version": 1,
                    "request_id": "write-1",
                    "operation": "set",
                    "values": {"value": 10},
                }
            ),
        )
        conflict, _ = observer.wait(
            "/one/commands/response", lambda p: p.get("status") == "rejected"
        )
        assert "different payload" in conflict["error"]
        observer.publish("/one/set", '{"value": 12}')
        legacy, _ = observer.wait("/one/set/response")
        assert legacy["results"]["value"]["status"] == "confirmed"
        observer.wait("/one/data", lambda p: p.get("value") == 12)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if snapshot.exists():
                exported = json.loads(snapshot.read_text())
                if exported.get("one", {}).get("value") == 12:
                    break
            time.sleep(0.05)
        else:
            pytest.fail("Aggregate snapshot not updated")
        assert exported["two"]["value"] == 23
        assert isinstance(exported["one"]["timestamp"], float)
        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=8) == 0
        observer.wait("/status", lambda p: p.get("online") is False)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        logfile.close()
        modbus.close()
        observer.close()
    assert "Unexpected gateway failure" not in (tmp_path / "gateway.log").read_text()


def test_automatic_broker_reconnect_republishes_online(mqtt_local_broker):
    clear_threading_event()
    broker = mqtt_local_broker
    prefix = "reconnect-test/" + uuid.uuid4().hex
    handler = MqttHandler(
        "reconnect",
        broker.host,
        broker.port,
        None,
        None,
        None,
        1,
        status_topic=prefix + "/status",
    )
    assert handler.setup() and handler.connect()
    broker.stop()
    try:
        deadline = time.monotonic() + 5
        while handler.is_connected() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not handler.is_connected()
        started = time.monotonic()
        assert handler.publish(prefix + "/data", "{}") is None
        assert time.monotonic() - started < 0.5
        broker.start()
        deadline = time.monotonic() + 10
        while not handler.is_connected() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert handler.is_connected()
        observer = Collector(broker.host, broker.port, prefix)
        try:
            payload, retained = observer.wait("/status")
            assert payload == {"online": True} and retained
        finally:
            observer.close()
    finally:
        broker.start() if broker.process.poll() is not None else None
        handler.close()


def test_simulator_separates_slaves_and_object_spaces(modbus_test_endpoint):
    client = ModbusTcpClient(modbus_test_endpoint.host, port=modbus_test_endpoint.port)
    assert client.connect()
    try:
        assert not client.write_register(80, 222, device_id=1).isError()
        assert not client.write_register(80, 333, device_id=2).isError()
        assert client.read_holding_registers(80, count=1, device_id=1).registers == [
            222
        ]
        assert client.read_holding_registers(80, count=1, device_id=2).registers == [
            333
        ]
        assert client.read_input_registers(80, count=1, device_id=1).registers == [0]
        assert not client.write_coil(80, True, device_id=1).isError()
        assert client.read_discrete_inputs(80, count=1, device_id=1).bits[0] is False
    finally:
        client.close()
