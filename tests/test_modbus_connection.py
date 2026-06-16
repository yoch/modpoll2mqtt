from unittest.mock import MagicMock

import pytest
from pymodbus.exceptions import ModbusException

from modpoll.modbus_connection import ModbusConnectionManager


class Clock:
    def __init__(self, now=100.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def test_connect_success_and_reuse_without_reconnect():
    clock = Clock()
    client = MagicMock()
    client.connect.return_value = True
    manager = ModbusConnectionManager(client, clock=clock)

    assert manager.execute("poll", lambda: "ok").ok is True
    assert manager.execute("write", lambda: "ok").ok is True

    assert client.connect.call_count == 1
    client.close.assert_not_called()
    assert manager.state == manager.READY


def test_connect_failure_enters_non_blocking_backoff():
    clock = Clock()
    client = MagicMock()
    client.connect.return_value = False
    manager = ModbusConnectionManager(
        client, backoff_base=5.0, backoff_max=60.0, clock=clock
    )

    first = manager.execute("poll", lambda: "never")
    second = manager.execute("poll", lambda: "never")

    assert first.ok is False
    assert first.skipped is True
    assert second.ok is False
    assert second.skipped is True
    assert client.connect.call_count == 1
    assert manager.state == manager.BACKOFF
    assert manager.backoff_until == 105.0


def test_backoff_expiry_allows_reconnect():
    clock = Clock()
    client = MagicMock()
    client.connect.side_effect = [False, True]
    manager = ModbusConnectionManager(client, backoff_base=5.0, clock=clock)

    assert manager.execute("poll", lambda: "never").ok is False
    clock.advance(5.0)
    result = manager.execute("poll", lambda: "ok")

    assert result.ok is True
    assert result.value == "ok"
    assert client.connect.call_count == 2
    assert manager.state == manager.READY
    assert manager.consecutive_failures == 0


@pytest.mark.parametrize("exc", [OSError("boom"), ModbusException("boom")])
def test_transaction_transport_failure_closes_and_backs_off(exc):
    clock = Clock()
    client = MagicMock()
    client.connect.return_value = True
    manager = ModbusConnectionManager(client, backoff_base=2.0, clock=clock)

    result = manager.execute("poll", lambda: (_ for _ in ()).throw(exc))

    assert result.ok is False
    assert result.callback_started is True
    client.close.assert_called_once()
    assert manager.state == manager.BACKOFF
    assert manager.transaction_failure_count == 1
    assert manager.backoff_until == 102.0


def test_success_after_failure_resets_failure_state():
    clock = Clock()
    client = MagicMock()
    client.connect.side_effect = [False, True]
    manager = ModbusConnectionManager(client, backoff_base=1.0, clock=clock)

    assert manager.execute("poll", lambda: "never").ok is False
    clock.advance(1.0)
    assert manager.execute("poll", lambda: "ok").ok is True

    assert manager.consecutive_failures == 0
    assert manager.last_error is None
    assert manager.backoff_until is None


def test_close_is_idempotent():
    client = MagicMock()
    manager = ModbusConnectionManager(client)

    manager.close("test")
    manager.close("test")

    assert client.close.call_count == 2
    assert manager.state == manager.DISCONNECTED


def test_diagnostics_include_connection_state():
    clock = Clock()
    client = MagicMock()
    client.connect.return_value = True
    manager = ModbusConnectionManager(client, clock=clock)

    manager.execute("poll", lambda: "ok")

    diagnostics = manager.diagnostics()
    assert diagnostics["modbus_connection_state"] == manager.READY
    assert diagnostics["modbus_connected"] is True
    assert diagnostics["modbus_connect_count"] == 1


def test_initial_diagnostics_are_safe_before_connect():
    client = MagicMock()
    manager = ModbusConnectionManager(client)

    diagnostics = manager.diagnostics()

    assert diagnostics == {
        "modbus_connection_state": manager.DISCONNECTED,
        "modbus_connected": False,
        "modbus_connected_since": None,
        "modbus_last_success_at": None,
        "modbus_last_failure_at": None,
        "modbus_last_error": None,
        "modbus_consecutive_failures": 0,
        "modbus_backoff_until": None,
        "modbus_connect_count": 0,
        "modbus_reconnect_count": 0,
        "modbus_transaction_failure_count": 0,
    }


@pytest.mark.parametrize("exc", [OSError("dial failed"), ModbusException("dial failed")])
def test_connect_exception_enters_backoff_without_callback(exc):
    clock = Clock()
    client = MagicMock()
    client.connect.side_effect = exc
    callback = MagicMock(return_value="never")
    manager = ModbusConnectionManager(client, backoff_base=3.0, clock=clock)

    result = manager.execute("poll", callback)

    assert result.ok is False
    assert result.skipped is True
    assert result.callback_started is False
    callback.assert_not_called()
    assert client.close.call_count == 0
    assert manager.state == manager.BACKOFF
    assert manager.last_error.startswith("connect failed:")
    assert "dial failed" in manager.last_error
    assert manager.backoff_until == 103.0


def test_callback_is_never_called_while_backoff_is_active():
    clock = Clock()
    client = MagicMock()
    client.connect.return_value = False
    callback = MagicMock(return_value="never")
    manager = ModbusConnectionManager(client, backoff_base=10.0, clock=clock)

    assert manager.execute("poll", callback).ok is False
    assert manager.execute("poll", callback).ok is False

    callback.assert_not_called()
    assert client.connect.call_count == 1


def test_exponential_backoff_is_capped():
    clock = Clock()
    client = MagicMock()
    client.connect.return_value = False
    manager = ModbusConnectionManager(
        client, backoff_base=2.0, backoff_max=5.0, clock=clock
    )

    expected_delays = [2.0, 4.0, 5.0, 5.0]
    for expected_delay in expected_delays:
        result = manager.execute("poll", lambda: "never")
        assert result.ok is False
        assert manager.backoff_until == clock.now + expected_delay
        clock.advance(expected_delay)

    assert client.connect.call_count == len(expected_delays)
    assert manager.consecutive_failures == len(expected_delays)


def test_successful_reconnect_counts_as_reconnect():
    clock = Clock()
    client = MagicMock()
    client.connect.side_effect = [False, True]
    manager = ModbusConnectionManager(client, backoff_base=1.0, clock=clock)

    assert manager.execute("poll", lambda: "never").ok is False
    clock.advance(1.0)
    assert manager.execute("poll", lambda: "ok").ok is True

    assert manager.connect_count == 2
    assert manager.reconnect_count == 1


def test_max_connection_age_recycles_before_next_transaction():
    clock = Clock()
    client = MagicMock()
    client.connect.return_value = True
    manager = ModbusConnectionManager(client, max_connection_age=5.0, clock=clock)

    assert manager.execute("poll", lambda: "first").ok is True
    clock.advance(4.999)
    assert manager.execute("poll", lambda: "still same").ok is True
    clock.advance(0.001)
    result = manager.execute("poll", lambda: "reconnected")

    assert result.ok is True
    assert result.value == "reconnected"
    assert client.connect.call_count == 2
    assert client.close.call_count == 1
    assert manager.reconnect_count == 1
    assert manager.connected_since == clock.now


def test_max_connection_age_disabled_does_not_recycle():
    clock = Clock()
    client = MagicMock()
    client.connect.return_value = True
    manager = ModbusConnectionManager(client, max_connection_age=None, clock=clock)

    assert manager.execute("poll", lambda: "first").ok is True
    clock.advance(10_000)
    assert manager.execute("poll", lambda: "same connection").ok is True

    assert client.connect.call_count == 1
    client.close.assert_not_called()


def test_close_suppresses_close_exceptions_and_disconnects():
    client = MagicMock()
    client.close.side_effect = OSError("close boom")
    manager = ModbusConnectionManager(client)
    manager.state = manager.READY
    manager.connected_since = 100.0

    manager.close("test")

    assert manager.state == manager.DISCONNECTED
    assert manager.connected_since is None


def test_close_with_no_client_is_safe():
    manager = ModbusConnectionManager(None)

    manager.close("test")

    assert manager.state == manager.DISCONNECTED
    assert manager.connected_since is None


def test_transaction_success_preserves_none_value():
    client = MagicMock()
    client.connect.return_value = True
    manager = ModbusConnectionManager(client)

    result = manager.execute("get", lambda: None)

    assert result.ok is True
    assert result.value is None
    assert result.callback_started is True


def test_non_transport_exception_is_not_swallowed():
    client = MagicMock()
    client.connect.return_value = True
    manager = ModbusConnectionManager(client)

    with pytest.raises(ValueError, match="bad decode"):
        manager.execute("poll", lambda: (_ for _ in ()).throw(ValueError("bad decode")))

    assert manager.state == manager.READY
    assert manager.transaction_failure_count == 0
