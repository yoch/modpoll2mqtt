import time

import pytest

from modpoll.mqtt_task import MqttHandler
from modpoll.utils import delay_thread


def _make_handler(
    mqtt_broker,
    client_id: str,
    *,
    qos: int = 0,
    subscribe_topics=None,
    mqtt_version: str = "5.0",
) -> MqttHandler:
    host, port = mqtt_broker
    return MqttHandler(
        name="integration",
        host=host,
        port=port,
        user=None,
        password=None,
        clientid=client_id,
        qos=qos,
        subscribe_topics=subscribe_topics or [],
        mqtt_version=mqtt_version,
    )


def _connect(handler: MqttHandler) -> None:
    assert handler.setup()
    assert handler.connect()
    assert handler.is_connected()


@pytest.mark.integration
@pytest.mark.mqtt
def test_mqtt_integration_connect(mqtt_broker, unique_mqtt_client_id):
    handler = _make_handler(mqtt_broker, unique_mqtt_client_id)
    try:
        _connect(handler)
    finally:
        handler.close()


@pytest.mark.integration
@pytest.mark.mqtt
def test_mqtt_integration_publish_qos0(
    mqtt_broker, unique_mqtt_client_id, unique_mqtt_topic
):
    handler = _make_handler(mqtt_broker, unique_mqtt_client_id, qos=0)
    try:
        _connect(handler)
        info = handler.publish(unique_mqtt_topic, "modpoll-integration-qos0")
        assert info is not None
        assert info.rc == 0
    finally:
        handler.close()


@pytest.mark.integration
@pytest.mark.mqtt
def test_mqtt_integration_publish_qos1(
    mqtt_broker, unique_mqtt_client_id, unique_mqtt_topic
):
    handler = _make_handler(mqtt_broker, unique_mqtt_client_id, qos=1)
    try:
        _connect(handler)
        info = handler.publish(unique_mqtt_topic, "modpoll-integration-qos1", qos=1)
        assert info is not None
        assert info.rc == 0
        info.wait_for_publish(timeout=10)
        assert info.is_published()
    finally:
        handler.close()


@pytest.mark.integration
@pytest.mark.mqtt
def test_mqtt_integration_subscribe_and_receive(
    mqtt_broker, unique_mqtt_client_id, unique_mqtt_topic
):
    handler = _make_handler(
        mqtt_broker,
        unique_mqtt_client_id,
        qos=1,
        subscribe_topics=[unique_mqtt_topic],
    )
    payload = b"modpoll-integration-rx"
    try:
        _connect(handler)
        handler.publish(unique_mqtt_topic, payload.decode(), qos=1)

        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            topic, received = handler.receive()
            if topic == unique_mqtt_topic and received == payload:
                break
            delay_thread(0.1)
        else:
            pytest.fail("timed out waiting for subscribed MQTT message")

        assert handler.receive() == (None, None)
    finally:
        handler.close()


@pytest.mark.integration
@pytest.mark.mqtt
def test_mqtt_integration_mqtt_v311_connect(mqtt_broker, unique_mqtt_client_id):
    handler = _make_handler(mqtt_broker, unique_mqtt_client_id, mqtt_version="3.1.1")
    try:
        _connect(handler)
    finally:
        handler.close()


@pytest.mark.integration
@pytest.mark.mqtt
def test_mqtt_integration_publish_reconnects_after_disconnect(
    mqtt_broker, unique_mqtt_client_id, unique_mqtt_topic
):
    """publish() with QoS>0 must reconnect when the broker session drops."""
    handler = _make_handler(mqtt_broker, unique_mqtt_client_id, qos=1)
    try:
        _connect(handler)
        handler.mqtt_client.disconnect()
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and handler.is_connected():
            delay_thread(0.1)
        assert not handler.is_connected()

        info = handler.publish(unique_mqtt_topic, "modpoll-after-disconnect", qos=1)
        assert info is not None
        assert info.rc == 0
        assert handler.is_connected()
    finally:
        handler.close()


@pytest.mark.integration
@pytest.mark.mqtt
def test_mqtt_integration_new_handler_after_close(mqtt_broker, unique_mqtt_topic):
    """close() tears down the loop; a fresh handler can connect and publish."""
    client_id = f"modpoll-test-{unique_mqtt_topic.rsplit('/', 1)[-1][:16]}"
    handler = _make_handler(mqtt_broker, client_id, qos=0)
    try:
        _connect(handler)
        handler.close()
        assert not handler.is_connected()
    finally:
        handler.close()

    handler2 = _make_handler(mqtt_broker, f"{client_id}-2", qos=0)
    try:
        _connect(handler2)
        info = handler2.publish(unique_mqtt_topic, "modpoll-second-session")
        assert info is not None
        assert info.rc == 0
    finally:
        handler2.close()
