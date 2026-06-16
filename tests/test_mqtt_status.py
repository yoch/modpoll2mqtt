import json
from unittest.mock import MagicMock

from paho.mqtt.client import ConnectFlags
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.reasoncodes import ReasonCode

from modpoll.mqtt_task import _MQTT_STATUS_TOPIC, MqttHandler


def _signal_connect(handler, rc=0):
    handler._on_connect(
        handler.mqtt_client,
        None,
        ConnectFlags(session_present=False),
        ReasonCode(PacketTypes.CONNACK, identifier=rc),
        None,
    )


def _stub_client_with_publish():
    stub = MagicMock()
    stub.is_connected.return_value = True
    stub.publish.return_value = MagicMock(rc=0)
    return stub


def test_connect_sets_lwt_on_status_topic(monkeypatch):
    handler = MqttHandler(
        name="test_mqtt",
        host="broker.local",
        port=1883,
        user=None,
        password=None,
        clientid="test_client",
        qos=1,
    )
    assert handler.setup()

    will_calls = []
    connected = [False]

    def fake_will_set(topic, payload, qos, retain):
        will_calls.append((topic, payload, qos, retain))

    def fake_connect_async(**kwargs):
        connected[0] = True
        _signal_connect(handler)

    handler.mqtt_client.will_set = fake_will_set
    handler.mqtt_client.loop_start = lambda: None
    handler.mqtt_client.connect_async = fake_connect_async
    handler.mqtt_client.is_connected = lambda: connected[0]
    monkeypatch.setattr("modpoll.mqtt_task.delay_thread", lambda timeout: None)

    assert handler.connect() is True
    assert will_calls == [
        (
            _MQTT_STATUS_TOPIC,
            json.dumps({"online": False}),
            1,
            True,
        )
    ]


def test_connect_publishes_online_true(monkeypatch):
    handler = MqttHandler(
        name="test_mqtt",
        host="broker.local",
        port=1883,
        user=None,
        password=None,
        clientid="test_client",
        qos=0,
    )
    assert handler.setup()

    connected = [False]
    publish_calls = []

    def fake_connect_async(**kwargs):
        connected[0] = True
        _signal_connect(handler)

    handler.mqtt_client.loop_start = lambda: None
    handler.mqtt_client.connect_async = fake_connect_async
    handler.mqtt_client.is_connected = lambda: connected[0]
    handler.mqtt_client.publish = lambda topic, msg, qos, retain: publish_calls.append(
        (topic, msg, qos, retain)
    ) or MagicMock(rc=0)
    monkeypatch.setattr("modpoll.mqtt_task.delay_thread", lambda timeout: None)

    assert handler.connect() is True
    assert publish_calls[-1] == (
        _MQTT_STATUS_TOPIC,
        json.dumps({"online": True}),
        0,
        True,
    )


def test_publish_status_always_retains():
    handler = MqttHandler(
        name="test_mqtt",
        host="broker.local",
        port=1883,
        user=None,
        password=None,
        clientid="test_client",
        qos=2,
        retain_data_publishes=False,
    )
    publish_calls = []
    handler.mqtt_client = MagicMock()
    handler.mqtt_client.is_connected.return_value = True
    handler.mqtt_client.publish = lambda topic, msg, qos, retain: publish_calls.append(
        (topic, msg, qos, retain)
    ) or MagicMock(rc=0)

    handler.publish_status(False)

    assert publish_calls == [
        (_MQTT_STATUS_TOPIC, json.dumps({"online": False}), 2, True)
    ]


def test_close_publishes_offline_before_disconnect():
    handler = MqttHandler(
        name="test_mqtt",
        host="broker.local",
        port=1883,
        user=None,
        password=None,
        clientid="test_client",
        qos=0,
    )
    calls = []
    handler.mqtt_client = MagicMock()
    handler.mqtt_client.is_connected.return_value = True
    handler.mqtt_client.publish = lambda topic, msg, qos, retain: calls.append(
        ("publish", topic, msg, retain)
    ) or MagicMock(rc=0)
    handler.mqtt_client.disconnect = lambda: calls.append(("disconnect",))
    handler.mqtt_client.loop_stop = lambda: calls.append(("loop_stop",))

    handler.close()

    assert calls[0][0] == "publish"
    assert calls[0][1] == _MQTT_STATUS_TOPIC
    assert calls[0][2] == json.dumps({"online": False})
    assert calls[0][3] is True
    assert ("disconnect",) in calls
