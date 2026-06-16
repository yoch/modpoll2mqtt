import json
import logging
import queue
import socket
import ssl
import time
from threading import Event

from paho.mqtt import MQTTException
from paho.mqtt.client import (
    CallbackAPIVersion,
    MQTTMessageInfo,
    MQTTProtocolVersion,
)
from paho.mqtt.client import (
    Client as MQTTClient,
)

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
        tls_version: str = "tlsv1.2",
        cacerts: str | None = None,
        insecure: bool = False,
        mqtt_version: str = "5.0",
        log_level: str = "INFO",
        rx_queue_size: int = 1000,
        retain_data_publishes: bool = False,
    ):
        self.name = name
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.clientid = clientid
        self.qos = qos
        self.subscribe_topics: list[str] = subscribe_topics or []
        self.use_tls = use_tls
        self.tls_version = tls_version
        self.cacerts = cacerts
        self.insecure = insecure
        self.mqtt_version = mqtt_version
        self.loglevel = log_level

        self.mqtt_client: MQTTClient | None = None
        self.clean_start_or_session = qos == 0
        self.rx_queue_size = rx_queue_size
        self.retain_data_publishes = retain_data_publishes
        self.rx_queue: queue.Queue = queue.Queue(maxsize=rx_queue_size)
        self._connected_event = Event()
        self._connect_rc: int | None = None
        self._closed = False
        self.logger = logging.getLogger(__name__)

    # Callback signatures follow paho CallbackAPIVersion.VERSION2: for both
    # MQTT v3.1.1 and v5, flags is a ConnectFlags and reason codes are ReasonCode.
    def _on_connect(self, client, userdata, flags, reason_code, properties):
        rc = reason_code.value
        self._connect_rc = rc

        if rc != 0:
            self.logger.warning(f"Connection failed with reason code: {rc}")
            self._connected_event.set()
            return

        self.logger.debug("Connected to MQTT broker.")
        if flags.session_present:
            self.logger.info("MQTT session present, reusing existing session.")
        else:
            self.logger.info("Created new MQTT session.")
        for topic in self.subscribe_topics:
            self.logger.info(f"Subscribe to topic: {topic} with QoS: {self.qos}")
            client.subscribe(topic, self.qos)
        self._connected_event.set()

    def _on_subscribe(self, client, userdata, mid, reason_codes, properties):
        for rc in reason_codes:
            if rc.is_failure:
                self.logger.warning(f"Failed to subscribe. Reason: {rc}")
            else:
                self.logger.info(f"Subscribed successfully (QoS={rc.value}).")

    def _on_publish(self, client, userdata, mid, reason_codes, properties):
        self.logger.debug(f"Message (mid={mid}) published successfully.")

    def _on_message(self, client, userdata, message):
        if message.retain == 0:
            self.logger.info(f"Receive message ({message.topic}): {message.payload}")
        else:
            self.logger.info(
                f"Receive retained message ({message.topic}): {message.payload}"
            )
        try:
            self.rx_queue.put((message.topic, message.payload), block=False)
        except queue.Full:
            self.logger.warning("MQTT receiving queue is full, ignoring new message.")

    def _on_disconnect(self, client, userdata, flags, reason_code, properties):
        if reason_code.value == 0:
            self.logger.info("Disconnected.")
        else:
            self.logger.warning(f"Disconnected with error, reason_code={reason_code}.")

    def _on_log(self, client, userdata, level, buf):
        self.logger.debug(f"{level} | {buf}")

    def _setup_tls(self) -> bool:
        try:
            tls_versions = {
                "tlsv1.2": getattr(ssl, "PROTOCOL_TLSv1_2", None),
                "tlsv1.1": getattr(ssl, "PROTOCOL_TLSv1_1", None),
                "tlsv1": getattr(ssl, "PROTOCOL_TLSv1", None),
            }
            key = self.tls_version.lower()
            if key in tls_versions:
                tls_version = tls_versions[key]
                if tls_version is None:
                    self.logger.error(
                        f"TLS version '{self.tls_version}' is not supported on this Python"
                    )
                    return False
            else:
                tls_version = ssl.PROTOCOL_TLS
            cert_required = ssl.CERT_NONE if self.insecure else ssl.CERT_REQUIRED
            self.mqtt_client.tls_set(
                ca_certs=self.cacerts,
                certfile=None,
                keyfile=None,
                cert_reqs=cert_required,
                tls_version=tls_version,
                ciphers=None,
            )
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
            clientid = self.clientid or ("" if self.qos == 0 else socket.gethostname())

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

        try:
            connect_kwargs: dict = {
                "host": self.host,
                "port": self.port,
                "keepalive": 60,
            }
            if self.mqtt_version == "5.0":
                connect_kwargs["clean_start"] = self.clean_start_or_session

            self.mqtt_client.will_set(
                _MQTT_STATUS_TOPIC,
                json.dumps({"online": False}),
                qos=self.qos,
                retain=True,
            )
            self.mqtt_client.connect_async(**connect_kwargs)
            self.mqtt_client.loop_start()
        except (OSError, MQTTException, TypeError, ValueError) as ex:
            self.logger.error(f"MQTT connection error: {ex}")
            self._stop_mqtt_loop()
            return False

        deadline = time.monotonic() + _MQTT_CONNECT_TIMEOUT
        while time.monotonic() < deadline:
            if self._connected_event.is_set():
                if self._connect_rc == 0 and self.mqtt_client.is_connected():
                    self.publish_status(True)
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
            _MQTT_STATUS_TOPIC,
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
            if qos == 0:
                self.logger.warning(
                    "MQTT client not connected and QoS is 0, skipping publish."
                )
                return None
            self.logger.warning("MQTT client not connected, attempting to reconnect.")
            if not self.connect():
                return None

        try:
            pubinfo = self.mqtt_client.publish(topic, msg, qos, retain)
            self.logger.debug(
                f"Publishing MQTT topic: {topic}, msg: {msg}, qos: {qos}, RC: {pubinfo.rc}"
            )
            self.logger.info(f"Publish message to topic: {topic}")
            return pubinfo
        except MQTTException as ex:
            self.logger.error(
                f"Failed to publish MQTT topic: {topic}, msg: {msg}, qos: {qos}. Error: {ex}"
            )
            return None

    def receive(self) -> tuple[str | None, bytes | None]:
        try:
            topic, payload = self.rx_queue.get(block=False)
            return topic, payload
        except queue.Empty:
            return None, None

    def is_connected(self) -> bool:
        return self.mqtt_client is not None and self.mqtt_client.is_connected()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self.mqtt_client:
            if self.mqtt_client.is_connected():
                self.publish_status(False)
            self._stop_mqtt_loop()
        else:
            self.logger.warning("MQTT client not initialized, nothing to close.")
