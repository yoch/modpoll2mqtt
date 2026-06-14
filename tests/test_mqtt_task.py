import pytest
import ssl
from paho.mqtt.client import ConnectFlags
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.reasoncodes import ReasonCode

from modpoll.mqtt_task import MqttHandler


def _signal_connect(handler, rc=0):
    """Simulate the paho VERSION2 on_connect dispatch (ConnectFlags + ReasonCode)."""
    handler._on_connect(
        handler.mqtt_client,
        None,
        ConnectFlags(session_present=False),
        ReasonCode(PacketTypes.CONNACK, identifier=rc),
        None,
    )


def test_mqtt_task_setup():
    mqtt_handler = MqttHandler(
        name="test_mqtt",
        host="broker.emqx.io",
        port=1883,
        user=None,
        password=None,
        clientid="test_client_13579",
        qos=0,
        subscribe_topics=["test/topic"],
    )

    assert mqtt_handler.setup()

    # Test MQTT client properties
    assert mqtt_handler.mqtt_client is not None
    assert mqtt_handler.host == "broker.emqx.io"
    assert mqtt_handler.port == 1883

    # Clean up
    mqtt_handler.close()


def test_rx_queue_respects_configured_size():
    handler = MqttHandler(
        name="test_mqtt",
        host="broker.emqx.io",
        port=1883,
        user=None,
        password=None,
        clientid="test_client_13579",
        qos=0,
        rx_queue_size=2,
    )

    assert handler.rx_queue.maxsize == 2


def test_receive_empty_queue_returns_none():
    handler = MqttHandler(
        name="test_mqtt",
        host="broker.emqx.io",
        port=1883,
        user=None,
        password=None,
        clientid="test_client_13579",
        qos=0,
    )

    topic, payload = handler.receive()

    assert topic is None
    assert payload is None


def test_receive_returns_message_when_available():
    handler = MqttHandler(
        name="test_mqtt",
        host="broker.emqx.io",
        port=1883,
        user=None,
        password=None,
        clientid="test_client_13579",
        qos=0,
    )

    class FakeQueue:
        def get(self, block=False):
            return ("some/topic", b"payload")

    handler.rx_queue = FakeQueue()

    topic, payload = handler.receive()

    assert topic == "some/topic"
    assert payload == b"payload"


def test_publish_when_connected_calls_client_publish():
    handler = MqttHandler(
        name="test_mqtt",
        host="broker.emqx.io",
        port=1883,
        user=None,
        password=None,
        clientid="test_client_13579",
        qos=1,
    )

    class StubClient:
        def __init__(self):
            self.calls = []

        def is_connected(self):
            return True

        def publish(self, topic, msg, qos, retain):
            self.calls.append((topic, msg, qos, retain))

            class PubInfo:
                rc = 0

            return PubInfo()

    stub = StubClient()
    handler.mqtt_client = stub

    result = handler.publish("topic", "message", qos=2, retain=True)

    assert result.rc == 0
    assert stub.calls == [("topic", "message", 2, True)]


@pytest.mark.parametrize(
    "retain_data_publishes,qos,expected_retain",
    [(False, 0, False), (True, 1, True), (False, 2, False)],
)
def test_publish_data_message_uses_handler_qos_and_retain(
    retain_data_publishes, qos, expected_retain
):
    handler = MqttHandler(
        name="test_mqtt",
        host="broker.emqx.io",
        port=1883,
        user=None,
        password=None,
        clientid="test_client_retain",
        qos=qos,
        retain_data_publishes=retain_data_publishes,
    )

    class StubClient:
        def __init__(self):
            self.calls = []

        def is_connected(self):
            return True

        def publish(self, topic, msg, qos, retain):
            self.calls.append((topic, msg, qos, retain))

            class PubInfo:
                rc = 0

            return PubInfo()

    stub = StubClient()
    handler.mqtt_client = stub

    handler.publish_data_message("data/topic", "payload")

    assert stub.calls == [("data/topic", "payload", qos, expected_retain)]


def test_mqtt_tlsv1_unsupported_returns_false_from_setup(monkeypatch):
    handler = MqttHandler(
        name="test_mqtt",
        host="broker.emqx.io",
        port=8883,
        user=None,
        password=None,
        clientid="client",
        qos=0,
        use_tls=True,
        tls_version="tlsv1",
        cacerts="/tmp/ca.pem",
        mqtt_version="3.1.1",
    )

    if hasattr(ssl, "PROTOCOL_TLSv1"):
        monkeypatch.delattr(ssl, "PROTOCOL_TLSv1", raising=False)

    assert handler.setup() is False


def test_setup_respects_tls_option(monkeypatch):
    called = {}
    handler = MqttHandler(
        name="test_mqtt",
        host="broker.emqx.io",
        port=8883,
        user=None,
        password=None,
        clientid="client",
        qos=0,
        subscribe_topics=["secure/topic"],
        use_tls=True,
        tls_version="tlsv1.2",
        mqtt_version="3.1.1",
    )

    def fake_setup_tls():
        called["tls"] = True
        return True

    monkeypatch.setattr(handler, "_setup_tls", fake_setup_tls)

    assert handler.setup()
    assert called.get("tls") is True
    assert handler.mqtt_client is not None


def test_publish_skips_when_disconnected_qos0(monkeypatch):
    handler = MqttHandler(
        name="test_mqtt",
        host="broker.emqx.io",
        port=1883,
        user=None,
        password=None,
        clientid="test_client_13579",
        qos=0,
    )

    class StubClient:
        def is_connected(self):
            return False

    handler.mqtt_client = StubClient()

    result = handler.publish("topic", "msg")

    assert result is None


def test_connect_mqtt_v311_omits_clean_session_kwarg(monkeypatch):
    """paho-mqtt 2.x rejects clean_session in connect(); it is set on the Client constructor."""
    handler = MqttHandler(
        name="test_mqtt",
        host="broker.local",
        port=1883,
        user=None,
        password=None,
        clientid="test_client",
        qos=0,
        mqtt_version="3.1.1",
    )

    assert handler.setup()

    captured = {}
    connected = [False]

    def fake_connect_async(**kwargs):
        captured.update(kwargs)
        connected[0] = True
        _signal_connect(handler)

    monkeypatch.setattr(handler.mqtt_client, "connect_async", fake_connect_async)
    monkeypatch.setattr(
        handler.mqtt_client,
        "loop_start",
        lambda: None,
    )
    monkeypatch.setattr(handler.mqtt_client, "is_connected", lambda: connected[0])

    assert handler.connect() is True
    assert "clean_session" not in captured
    assert captured == {
        "host": "broker.local",
        "port": 1883,
        "keepalive": 60,
    }


def test_publish_attempts_reconnect_when_disconnected_qos1(monkeypatch):
    handler = MqttHandler(
        name="test_mqtt",
        host="broker.emqx.io",
        port=1883,
        user=None,
        password=None,
        clientid="test_client_13579",
        qos=1,
    )

    class StubClient:
        def is_connected(self):
            return False

    handler.mqtt_client = StubClient()

    called = {}

    def fake_connect():
        called["connect"] = True
        return False

    monkeypatch.setattr(handler, "connect", fake_connect)

    result = handler.publish("topic", "msg")

    assert result is None
    assert called.get("connect") is True


def test_connect_returns_false_when_not_connected(monkeypatch):
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

    disconnect_called = []

    handler.mqtt_client.loop_start = lambda: None
    handler.mqtt_client.connect_async = lambda **kwargs: None
    handler.mqtt_client.is_connected = lambda: False
    handler.mqtt_client.disconnect = lambda *args, **kwargs: disconnect_called.append(
        True
    )
    handler.mqtt_client.loop_stop = lambda: None

    tick = [0.0]

    def fake_monotonic():
        tick[0] += 5.0
        return tick[0]

    monkeypatch.setattr("modpoll.mqtt_task.time.monotonic", fake_monotonic)
    monkeypatch.setattr("modpoll.mqtt_task.delay_thread", lambda timeout: None)

    assert handler.connect() is False
    assert disconnect_called


def test_connect_returns_true_when_is_connected(monkeypatch):
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

    checks = [0]

    def fake_connect_async(**kwargs):
        checks[0] += 1
        _signal_connect(handler)

    handler.mqtt_client.loop_start = lambda: None
    handler.mqtt_client.connect_async = fake_connect_async
    handler.mqtt_client.is_connected = lambda: checks[0] == 1

    monkeypatch.setattr("modpoll.mqtt_task.delay_thread", lambda timeout: None)

    assert handler.connect() is True


def test_connect_stops_previous_loop_before_reconnecting(monkeypatch):
    """connect() must terminate any previous network loop (disconnect + loop_stop)
    before reusing the same client for a fresh connect_async/loop_start cycle."""
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

    calls = []
    connected = [False]

    def fake_connect_async(**kwargs):
        connected[0] = True
        calls.append("connect_async")
        _signal_connect(handler)

    handler.mqtt_client.disconnect = lambda: calls.append("disconnect")
    handler.mqtt_client.loop_stop = lambda: calls.append("loop_stop")
    handler.mqtt_client.connect_async = fake_connect_async
    handler.mqtt_client.loop_start = lambda: calls.append("loop_start")
    handler.mqtt_client.is_connected = lambda: connected[0]
    monkeypatch.setattr("modpoll.mqtt_task.delay_thread", lambda timeout: None)

    assert handler.connect() is True
    assert calls == ["disconnect", "loop_stop", "connect_async", "loop_start"]


def test_connect_starts_loop_when_network_thread_absent(monkeypatch):
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

    handler.mqtt_client.loop_start = lambda: None
    handler.mqtt_client.connect_async = lambda **kwargs: _signal_connect(handler)
    handler.mqtt_client.is_connected = lambda: True

    monkeypatch.setattr("modpoll.mqtt_task.delay_thread", lambda timeout: None)

    assert handler.connect() is True


def test_connect_returns_false_when_connack_rejected(monkeypatch):
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

    disconnect_called = []

    handler.mqtt_client.loop_start = lambda: None
    handler.mqtt_client.connect_async = lambda **kwargs: _signal_connect(
        handler, rc=135  # CONNACK "Not authorized"
    )
    handler.mqtt_client.is_connected = lambda: False
    handler.mqtt_client.disconnect = lambda *args, **kwargs: disconnect_called.append(
        True
    )
    handler.mqtt_client.loop_stop = lambda: None

    monkeypatch.setattr("modpoll.mqtt_task.delay_thread", lambda timeout: None)

    assert handler.connect() is False
    # disconnect is called once before the attempt and once after the rejection
    assert disconnect_called == [True, True]


def test_connect_returns_false_after_close(monkeypatch):
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

    loop_start_calls = []
    handler.mqtt_client.loop_start = lambda: loop_start_calls.append(True)

    handler.close()

    assert handler.connect() is False
    assert loop_start_calls == []


@pytest.mark.integration
@pytest.mark.mqtt
def test_mqtt_task_connect(mqtt_broker, unique_mqtt_client_id):
    host, port = mqtt_broker
    mqtt_handler = MqttHandler(
        name="test_mqtt",
        host=host,
        port=port,
        user=None,
        password=None,
        clientid=unique_mqtt_client_id,
        qos=0,
    )

    assert mqtt_handler.setup()
    assert mqtt_handler.connect()
    assert mqtt_handler.is_connected()

    mqtt_handler.close()
