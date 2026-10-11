import os
import shutil
import socket
import subprocess
import time
import uuid

import pytest
from tests.modbus_integration import (
    ModbusEndpoint,
    ensure_modbus_test_server,
    get_modbus_test_endpoint,
    stop_modbus_test_server_process,
    tcp_reachable,
)


def _integration_unavailable(reason):
    if os.environ.get("MODPOLL_REQUIRE_INTEGRATION") == "1":
        pytest.fail(reason)
    pytest.skip(reason)


class LocalBroker:
    def __init__(self, executable, directory):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]
        self.host = "127.0.0.1"
        self.executable = executable
        self.config = directory / "mosquitto.conf"
        self.config.write_text(
            f"listener {self.port} 127.0.0.1\nallow_anonymous true\npersistence false\n"
        )
        self.process = None

    def start(self):
        self.process = subprocess.Popen(
            [self.executable, "-c", str(self.config)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError("Local Mosquitto exited before startup")
            if tcp_reachable(self.host, self.port, timeout=0.1):
                return
            time.sleep(0.05)
        raise RuntimeError("Local Mosquitto startup timeout")

    def stop(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)


@pytest.fixture(scope="session")
def mqtt_local_broker(tmp_path_factory):
    executable = os.environ.get("MOSQUITTO_BIN") or shutil.which("mosquitto")
    if not executable:
        _integration_unavailable(
            "Install Mosquitto or set MOSQUITTO_BIN for local MQTT tests"
        )
    broker = LocalBroker(executable, tmp_path_factory.mktemp("mqtt-broker"))
    try:
        broker.start()
        yield broker
    finally:
        broker.stop()


@pytest.fixture(scope="session")
def mqtt_broker(request):
    """Local ephemeral broker by default; remote endpoints require an explicit override."""
    host = os.environ.get("MQTT_TEST_HOST")
    if host:
        port = int(os.environ.get("MQTT_TEST_PORT", "1883"))
        if not tcp_reachable(host, port):
            _integration_unavailable(f"MQTT broker unavailable at {host}:{port}")
        return host, port
    broker = request.getfixturevalue("mqtt_local_broker")
    return broker.host, broker.port


@pytest.fixture(scope="session")
def modbus_test_endpoint() -> ModbusEndpoint:
    """Modbus TCP endpoint for integration tests; starts a local simulator if none is listening."""
    endpoint = get_modbus_test_endpoint()
    server_proc = ensure_modbus_test_server(endpoint)
    if server_proc is None and not tcp_reachable(endpoint.host, endpoint.port):
        _integration_unavailable(
            f"Modbus TCP unavailable at {endpoint.host}:{endpoint.port} "
            "and embedded test server could not be started "
            "(set MODBUS_TEST_HOST / MODBUS_TEST_PORT, or run "
            "`poetry run python tests/modbus_integration.py`)"
        )
    yield endpoint
    if server_proc is not None:
        stop_modbus_test_server_process(server_proc)


@pytest.fixture
def unique_mqtt_client_id():
    return f"modpoll-test-{uuid.uuid4().hex[:16]}"


@pytest.fixture
def unique_mqtt_topic():
    return f"modpoll/integration/{uuid.uuid4().hex}"
