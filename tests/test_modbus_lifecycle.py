from unittest.mock import MagicMock

from modpoll.modbus_connection import ModbusConnectionManager
from modpoll.modbus_task import Device, ModbusHandler, Poller


class Clock:
    def __init__(self, now=100.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def _handler_with_empty_poll(client):
    handler = ModbusHandler(client, "dummy.csv", no_output=True)
    device = Device("dev", 1)
    device.pollerList = []
    handler.deviceList = [device]
    return handler


def test_shared_client_single_connect_and_close_for_persistent_lifecycle():
    client = MagicMock()
    client.connect.return_value = True
    manager = ModbusConnectionManager(client)

    h1 = _handler_with_empty_poll(client)
    h2 = _handler_with_empty_poll(client)

    assert manager.execute("poll", h1.poll).ok is True
    assert manager.execute("poll", h2.poll).ok is True
    manager.close("shutdown")

    assert client.connect.call_count == 1
    assert client.close.call_count == 1


def test_on_poll_unavailable_clears_poll_success():
    client = MagicMock()
    handler = _handler_with_empty_poll(client)
    handler.deviceList[0].pollSuccess = True

    handler.on_poll_unavailable()

    assert handler.deviceList[0].pollSuccess is False


def test_on_poll_unavailable_autoremove_after_three_cycles():
    client = MagicMock()
    device = Device("dev", 1)
    poller = Poller(device, 3, 0, 1, "BE_BE")
    device.pollerList = [poller]

    handler = ModbusHandler(client, "dummy.csv", autoremove=True, no_output=True)
    handler.deviceList = [device]

    for _ in range(3):
        handler.on_poll_unavailable()

    assert poller.disabled is True


def test_connection_manager_backoff_is_non_blocking_after_failure():
    clock = Clock()
    client = MagicMock()
    client.connect.return_value = False
    manager = ModbusConnectionManager(client, backoff_base=5.0, clock=clock)
    callback = MagicMock()

    first = manager.execute("poll", callback)
    second = manager.execute("poll", callback)

    assert first.ok is False
    assert first.skipped is True
    assert second.ok is False
    assert second.skipped is True
    callback.assert_not_called()
    assert client.connect.call_count == 1
    assert manager.backoff_until == 105.0


def test_connection_manager_resets_backoff_on_success():
    clock = Clock()
    client = MagicMock()
    client.connect.side_effect = [False, True]
    manager = ModbusConnectionManager(client, backoff_base=1.0, clock=clock)

    assert manager.execute("poll", lambda: "never").ok is False
    clock.advance(1.0)
    assert manager.execute("poll", lambda: "ok").ok is True

    assert manager.consecutive_failures == 0
    assert manager.backoff_until is None
