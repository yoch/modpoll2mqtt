import pytest  # type: ignore
from modpoll.arg_parser import get_parser
from modpoll.modbus_connection import ModbusConnectionManager
from modpoll.modbus_task import setup_modbus_handlers

from tests.modbus_integration import ModbusEndpoint


def _modbus_args(endpoint: ModbusEndpoint, *config_paths: str) -> list[str]:
    return [
        "--config",
        *config_paths,
        "--tcp",
        endpoint.host,
        "--tcp-port",
        str(endpoint.port),
    ]


@pytest.mark.integration
@pytest.mark.modbus
def test_modbus_task_modbus_setup(modbus_test_endpoint):
    parser = get_parser()
    args = parser.parse_args(
        _modbus_args(
            modbus_test_endpoint,
            "examples/modsim.csv",
            "examples/modsim2.csv",
        )
    )
    _modbus_client, modbus_handlers = setup_modbus_handlers(args)
    assert len(modbus_handlers) == 2


@pytest.mark.integration
@pytest.mark.modbus
def test_modbus_task_poll_modsim(modbus_test_endpoint):
    parser = get_parser()
    args = parser.parse_args(_modbus_args(modbus_test_endpoint, "examples/modsim.csv"))
    modbus_client, modbus_handlers = setup_modbus_handlers(args)
    modbus_handler = modbus_handlers[0]

    manager = ModbusConnectionManager(modbus_client)
    try:
        assert manager.execute("poll", modbus_handler.poll).ok is True
    finally:
        manager.close("test")

    assert len(modbus_handler.deviceList) > 0
    assert len(modbus_handler.deviceList[0].references) > 0
    assert any(
        ref.val is not None for ref in modbus_handler.deviceList[0].references.values()
    )
