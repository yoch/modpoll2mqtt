from __future__ import annotations

import json
import logging
import queue
import ssl
import time
import uuid
from threading import Event, Lock
from typing import Any

from paho.mqtt import MQTTException
from paho.mqtt.client import Client as MQTTClient
from paho.mqtt.client import MQTTMessageInfo
from paho.mqtt.enums import CallbackAPIVersion, MQTTProtocolVersion
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.properties import Properties
from paho.mqtt.subscribeoptions import SubscribeOptions

from .commands import decode_json
from .utils import delay_thread, on_threading_event

_MQTT_CONNECT_TIMEOUT = 10.0
_MQTT_STATUS_TOPIC = "modpoll/status"


class MqttHandler:
    def __init__(
        self,
        name: str,
        host: str,
        port: int,
        user: str | None,
        password: str | None,
        clientid: str | None,
        qos: int,
        subscribe_topics: list[str] | None = None,
        use_tls: bool = False,
        cacerts: str | None = None,
        insecure: bool = False,
        mqtt_version: str = "5.0",
        log_level: str = "INFO",
        rx_queue_size: int = 1000,
        retain_data_publishes: bool = False,
        status_topic: str = _MQTT_STATUS_TOPIC,
        max_payload_bytes: int = 65536,
        queue_bytes: int = 1048576,
        command_ttl: float = 30.0,
        tx_queue_size: int = 1000,
        shutdown_timeout: float = 3.0,
        session_expiry: int = 0,
    ) -> None:
        self.name = name
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.clientid = clientid
        self.qos = qos
        self.subscribe_topics: list[str] = subscribe_topics or []
        self.use_tls = use_tls
        self.cacerts = cacerts
        self.insecure = insecure
        self.mqtt_version = mqtt_version
        self.loglevel = log_level

        self.mqtt_client: MQTTClient | None = None
        self.clean_start_or_session = session_expiry == 0
        self.session_expiry = session_expiry
        self.status_topic = status_topic
        self.max_payload_bytes = max_payload_bytes
        self.queue_bytes = queue_bytes
        self.command_ttl = command_ttl
        self.tx_queue_size = tx_queue_size
        self.shutdown_timeout = shutdown_timeout
        self._rejections: queue.Queue[tuple[str, bytes, str]] = queue.Queue(
            maxsize=rx_queue_size
        )
        self._queued_bytes = 0
        self._rx_lock = Lock()
        self.wakeup = Event()
        self._pending: list[MQTTMessageInfo] = []
        self._pending_lock = Lock()
        self.dropped_messages = 0
        self.expired_messages = 0
        self.publish_errors = 0
        self.subscription_errors = 0
        self.retain_data_publishes = retain_data_publishes
        self.rx_queue: queue.Queue[tuple[str, bytes, float]] = queue.Queue(
            maxsize=rx_queue_size
        )
        self._connected_event = Event()
        self._connect_rc: int | None = None
        # Bumped on every successful CONNACK so data publishers can detect
        # reconnections they did not observe as a status transition.
        self._connection_generation = 0
        self._closed = False
        self.logger = logging.getLogger(__name__)

    # Callback signatures follow paho CallbackAPIVersion.VERSION2: for both
    # MQTT v3.1.1 and v5, flags is a ConnectFlags and reason codes are ReasonCode.
    def _on_connect(
        self,
        client: MQTTClient,
        userdata: Any,
        flags: Any,
        reason_code: Any,
        properties: Any,
    ) -> None:
        rc = reason_code.value
        self._connect_rc = rc

        if rc != 0:
            self.logger.warning(f"Connection failed with reason code: {rc}")
            self._connected_event.set()
            return

        self.logger.debug("Connected to MQTT broker.")
        self._connection_generation += 1
        if flags.session_present:
            self.logger.info("MQTT session present, reusing existing session.")
        else:
            self.logger.info("Created new MQTT session.")
        for topic in self.subscribe_topics:
            self.logger.info(f"Subscribe to topic: {topic} with QoS: {self.qos}")
            if self.mqtt_version == "5.0":
                rc, _ = client.subscribe(
                    topic,
                    options=SubscribeOptions(qos=self.qos, retainAsPublished=True),
                )
            else:
                rc, _ = client.subscribe(topic, self.qos)
            if rc != 0:
                self.subscription_errors += 1
        self.publish_status(True)
        self._connected_event.set()
        self.wakeup.set()

    def _on_subscribe(
        self,
        client: MQTTClient,
        userdata: Any,
        mid: int,
        reason_codes: Any,
        properties: Any,
    ) -> None:
        for rc in reason_codes:
            if rc.is_failure:
                self.subscription_errors += 1
                self.logger.warning(f"Failed to subscribe. Reason: {rc}")
            else:
                self.logger.info(f"Subscribed successfully (QoS={rc.value}).")

    def _on_publish(
        self,
        client: MQTTClient,
        userdata: Any,
        mid: int,
        reason_codes: Any,
        properties: Any,
    ) -> None:
        if reason_codes is not None and reason_codes.is_failure:
            self.publish_errors += 1
            self.logger.warning("Broker rejected publication mid=%s", mid)
        else:
            self.logger.debug("Message mid=%s acknowledged", mid)

    def _on_message(self, client: MQTTClient, userdata: Any, message: Any) -> None:
        if self._closed or on_threading_event():
            return
        payload: bytes = message.payload
        if message.retain or len(payload) > self.max_payload_bytes:
            self.dropped_messages += 1
            if len(payload) <= self.max_payload_bytes:
                self._reject(message.topic, payload, "retained_command")
            self.logger.warning(
                "Rejected retained/oversized command on %s", message.topic
            )
            return
        with self._rx_lock:
            if self._queued_bytes + len(payload) > self.queue_bytes:
                self.dropped_messages += 1
                self._reject(message.topic, payload, "queue_byte_budget")
                self.logger.warning("MQTT receiving byte budget exceeded")
                return
            try:
                self.rx_queue.put_nowait((message.topic, payload, time.monotonic()))
            except queue.Full:
                self.dropped_messages += 1
                self._reject(message.topic, payload, "queue_full")
                self.logger.warning(
                    "MQTT receiving queue is full, ignoring new message"
                )
                return
            self._queued_bytes += len(payload)
            self.wakeup.set()

    def _reject(self, topic: str, payload: bytes, reason: str) -> None:
        # Callback work is bounded; decoding and responses belong to the owner loop.
        # Keep only small rejected payloads so this queue cannot double the byte budget.
        if len(payload) > min(self.max_payload_bytes, 2048):
            return
        try:
            self._rejections.put_nowait((topic, payload, reason))
            self.wakeup.set()
        except queue.Full:
            pass

    def _publish_rejection(self, topic: str, payload: bytes, reason: str) -> None:
        try:
            obj = decode_json(payload, self.max_payload_bytes)
            identifier = obj.get("request_id")
            if obj.get("version") == 1 and isinstance(identifier, str):
                self.publish_response(
                    topic + "/response",
                    json.dumps(
                        {
                            "version": 1,
                            "request_id": identifier,
                            "status": "rejected",
                            "error": reason,
                        }
                    ),
                )
        except ValueError:
            pass

    def _on_disconnect(
        self,
        client: MQTTClient,
        userdata: Any,
        flags: Any,
        reason_code: Any,
        properties: Any,
    ) -> None:
        if reason_code.value == 0:
            self.logger.info("Disconnected.")
        else:
            self.logger.warning(f"Disconnected with error, reason_code={reason_code}.")
        # No generation bump here: the next successful CONNACK invalidates
        # publisher caches, and nothing can be published while disconnected.

    def _on_log(self, client: MQTTClient, userdata: Any, level: int, buf: str) -> None:
        self.logger.debug(f"{level} | {buf}")

    def _connect_succeeded(self) -> bool:
        return (
            self._connect_rc == 0
            and self.mqtt_client is not None
            and self.mqtt_client.is_connected()
        )

    def _setup_tls(self) -> bool:
        if self.mqtt_client is None:
            return False
        try:
            context = ssl.create_default_context(cafile=self.cacerts)
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            if self.insecure:
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE
                self.logger.warning("MQTT server certificate verification disabled")
            self.mqtt_client.tls_set_context(context)  # pyright: ignore[reportUnknownMemberType] - upstream context stub
            return True
        except ssl.SSLError as ssl_ex:
            self.logger.error(f"SSL setup error: {ssl_ex}")
            raise
        except Exception as ex:
            self.logger.error(f"TLS setup error: {ex}")
            raise

    def setup(self) -> bool:
        self._closed = False
        try:
            clientid = self.clientid or f"modpoll-{uuid.uuid4().hex}"

            if self.mqtt_version == "5.0":
                self.mqtt_client = MQTTClient(
                    CallbackAPIVersion.VERSION2,
                    client_id=clientid,
                    protocol=MQTTProtocolVersion.MQTTv5,
                )
            else:
                self.mqtt_client = MQTTClient(
                    CallbackAPIVersion.VERSION2,
                    client_id=clientid,
                    clean_session=self.clean_start_or_session,
                    protocol=MQTTProtocolVersion.MQTTv311,
                )

            self.mqtt_client.max_queued_messages_set(self.tx_queue_size)
            self.mqtt_client.reconnect_delay_set(min_delay=1, max_delay=60)
            if self.use_tls and not self._setup_tls():
                return False

            if self.user:
                self.mqtt_client.username_pw_set(self.user, self.password)

            self.mqtt_client.on_connect = self._on_connect
            self.mqtt_client.on_subscribe = self._on_subscribe
            self.mqtt_client.on_message = self._on_message
            self.mqtt_client.on_publish = self._on_publish
            self.mqtt_client.on_disconnect = self._on_disconnect
            if self.loglevel.upper() == "DEBUG":
                self.mqtt_client.on_log = self._on_log

            return True
        except Exception as ex:
            self.logger.error(f"MQTT client setup error: {ex}")
            return False

    def _stop_mqtt_loop(self) -> None:
        """Terminate the network loop: disconnect() makes the paho thread exit,
        loop_stop() joins it. Both are harmless no-ops when nothing is running."""
        if self.mqtt_client:
            try:
                self.mqtt_client.disconnect()
                self.mqtt_client.loop_stop()
            except MQTTException as ex:
                self.logger.error(f"Error stopping MQTT loop: {ex}")

    def start(self) -> bool:
        """Start Paho's reconnecting network loop without blocking the poll owner."""
        if not self.mqtt_client or self._closed:
            return False
        try:
            self.mqtt_client.will_set(
                self.status_topic,
                json.dumps({"online": False}),
                qos=self.qos,
                retain=True,
            )
            if self.mqtt_version == "5.0":
                properties = Properties(PacketTypes.CONNECT)
                properties.SessionExpiryInterval = self.session_expiry
                self.mqtt_client.connect_async(
                    host=self.host,
                    port=self.port,
                    keepalive=60,
                    clean_start=self.clean_start_or_session,
                    properties=properties,
                )
            else:
                self.mqtt_client.connect_async(
                    host=self.host, port=self.port, keepalive=60
                )
            self.mqtt_client.loop_start()
            return True
        except (OSError, MQTTException, TypeError, ValueError) as exc:
            self.logger.error("MQTT connection error: %s", exc)
            self._stop_mqtt_loop()
            return False

    def connect(self) -> bool:
        if not self.mqtt_client:
            self.logger.error("MQTT client not initialized. Call setup() first.")
            return False

        if self._closed:
            self.logger.warning(
                "MQTT client is closed. Call setup() before reconnecting."
            )
            return False

        if self.mqtt_client.is_connected():
            return True

        # After loop_stop() the same client can be reused for a fresh
        # connect_async() + loop_start() cycle.
        self._stop_mqtt_loop()
        self._connected_event.clear()
        self._connect_rc = None

        if not self.start():
            return False

        deadline = time.monotonic() + _MQTT_CONNECT_TIMEOUT
        while time.monotonic() < deadline:
            if self._connected_event.is_set():
                if self._connect_succeeded():
                    return True
                self._stop_mqtt_loop()
                return False
            if on_threading_event():
                self.logger.info("Connection attempt interrupted by stop event.")
                self._stop_mqtt_loop()
                return False
            delay_thread(0.1)

        self.logger.error("MQTT connect timeout waiting for CONNACK.")
        self._stop_mqtt_loop()
        return False

    def publish_status(self, online: bool) -> MQTTMessageInfo | None:
        return self.publish(
            self.status_topic,
            json.dumps({"online": online}),
            qos=self.qos,
            retain=True,
        )

    def publish_data_message(self, topic: str, msg: str) -> MQTTMessageInfo | None:
        return self.publish(topic, msg, qos=self.qos, retain=self.retain_data_publishes)

    def publish(
        self, topic: str, msg: str, qos: int | None = None, retain: bool = False
    ) -> MQTTMessageInfo | None:
        if not self.mqtt_client:
            self.logger.error("MQTT client not initialized. Call setup() first.")
            return None

        qos = self.qos if qos is None else qos

        if not self.mqtt_client.is_connected():
            self.publish_errors += 1
            self.logger.warning("MQTT disconnected; publication skipped")
            return None

        try:
            with self._pending_lock:
                self._pending = [
                    info for info in self._pending if not info.is_published()
                ]
                if len(self._pending) >= self.tx_queue_size:
                    self.publish_errors += 1
                    return None
                pubinfo = self.mqtt_client.publish(topic, msg, qos, retain)
                if pubinfo.rc == 0:
                    self._pending.append(pubinfo)
            self.logger.debug(
                f"Publishing MQTT topic: {topic}, msg: {msg}, qos: {qos}, RC: {pubinfo.rc}"
            )
            if pubinfo.rc != 0:
                self.publish_errors += 1
                return None
            self.logger.debug("Accepted MQTT publish to %s", topic)
            return pubinfo
        except (MQTTException, ValueError, TypeError) as ex:
            self.publish_errors += 1
            self.logger.error(
                f"Failed to publish MQTT topic: {topic}, msg: {msg}, qos: {qos}. Error: {ex}"
            )
            return None

    def receive(self) -> tuple[str | None, bytes | None]:
        # Drain only a bounded number before serving normal commands.
        for _ in range(10):
            try:
                topic, payload, reason = self._rejections.get_nowait()
                self._publish_rejection(topic, payload, reason)
            except queue.Empty:
                break
        while True:
            try:
                with self._rx_lock:
                    topic, payload, received_at = self.rx_queue.get(block=False)
                    self._queued_bytes = max(0, self._queued_bytes - len(payload))
                    if self.rx_queue.empty():
                        self.wakeup.clear()
                if time.monotonic() - received_at > self.command_ttl:
                    self.expired_messages += 1
                    self._publish_rejection(topic, payload, "queue_expired")
                    continue
                return topic, payload
            except queue.Empty:
                return None, None

    def publish_response(self, topic: str, msg: str) -> MQTTMessageInfo | None:
        return self.publish(topic, msg, retain=False)

    def wait(self, timeout: float) -> None:
        self.wakeup.wait(min(timeout, 0.1))

    def flush(self, timeout: float | None = None) -> bool:
        deadline = time.monotonic() + (
            self.shutdown_timeout if timeout is None else timeout
        )
        with self._pending_lock:
            pending = list(self._pending)
        for info in pending:
            if info.is_published():
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            try:
                info.wait_for_publish(timeout=remaining)
                if not info.is_published():
                    return False
            except (RuntimeError, ValueError, MQTTException):
                return False
        return True

    def is_connected(self) -> bool:
        return self.mqtt_client is not None and self.mqtt_client.is_connected()

    @property
    def connection_generation(self) -> int:
        """Count of successful MQTT connections, for publisher cache invalidation."""
        return self._connection_generation

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self.mqtt_client:
            if self.mqtt_client.is_connected():
                self.publish_status(False)
                self.flush()
            self._stop_mqtt_loop()
        else:
            self.logger.warning("MQTT client not initialized, nothing to close.")
