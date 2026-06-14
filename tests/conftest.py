import os
import uuid

import pytest

from tests.modbus_integration import (
    ModbusEndpoint,
    ensure_modbus_test_server,
    get_modbus_test_endpoint,
    stop_modbus_test_server_process,
    tcp_reachable,
)

INTEGRATION_MQTT_HOST = os.environ.get("MQTT_TEST_HOST", "broker.emqx.io")
INTEGRATION_MQTT_PORT = int(os.environ.get("MQTT_TEST_PORT", "1883"))


def _mqtt_broker_reachable(host: str, port: int, timeout: float = 3.0) -> bool:
    return tcp_reachable(host, port, timeout=timeout)


@pytest.fixture(scope="session")
def mqtt_broker():
    """Public broker used by MQTT integration tests (override via MQTT_TEST_HOST/PORT)."""
    if not _mqtt_broker_reachable(INTEGRATION_MQTT_HOST, INTEGRATION_MQTT_PORT):
        pytest.skip(
            f"MQTT broker unavailable at {INTEGRATION_MQTT_HOST}:{INTEGRATION_MQTT_PORT}"
        )
    return INTEGRATION_MQTT_HOST, INTEGRATION_MQTT_PORT


@pytest.fixture(scope="session")
def modbus_test_endpoint() -> ModbusEndpoint:
    """Modbus TCP endpoint for integration tests; starts a local simulator if none is listening."""
    endpoint = get_modbus_test_endpoint()
    server_proc = ensure_modbus_test_server(endpoint)
    if server_proc is None and not tcp_reachable(endpoint.host, endpoint.port):
        pytest.skip(
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
