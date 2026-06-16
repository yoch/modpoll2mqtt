"""Persistent Modbus connection lifecycle management."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Optional

from pymodbus.exceptions import ModbusException

from .utils import get_utc_time

MODBUS_BACKOFF_BASE = 1.0
MODBUS_BACKOFF_MAX = 60.0


@dataclass
class TransactionResult:
    ok: bool
    value: Any = None
    skipped: bool = False
    callback_started: bool = False
    error: Optional[str] = None


class ModbusConnectionManager:
    """Keep one Modbus client open while bounding reconnect and failure paths."""

    DISCONNECTED = "DISCONNECTED"
    CONNECTING = "CONNECTING"
    READY = "READY"
    BACKOFF = "BACKOFF"
    CLOSING = "CLOSING"

    def __init__(
        self,
        client,
        *,
        backoff_base: float = MODBUS_BACKOFF_BASE,
        backoff_max: float = MODBUS_BACKOFF_MAX,
        max_connection_age: Optional[float] = None,
        clock: Callable[[], float] = get_utc_time,
        logger: Optional[logging.Logger] = None,
    ):
        self.client = client
        self.backoff_base = backoff_base
        self.backoff_max = backoff_max
        self.max_connection_age = max_connection_age
        self.clock = clock
        self.logger = logger or logging.getLogger(__name__)

        self.state = self.DISCONNECTED
        self.connected_since = None
        self.last_success_at = None
        self.last_failure_at = None
        self.last_error = None
        self.backoff_until = None
        self.consecutive_failures = 0
        self.connect_count = 0
        self.reconnect_count = 0
        self.transaction_failure_count = 0
        self._has_connected = False

    @property
    def connected(self) -> bool:
        return self.state == self.READY

    def ensure_connected(self, now: Optional[float] = None) -> bool:
        now = self.clock() if now is None else now

        if self.state == self.READY:
            if self._connection_expired(now):
                self.close("max connection age reached")
            else:
                return True

        if self.state == self.BACKOFF and self.backoff_until is not None:
            if now < self.backoff_until:
                return False

        previous_state = self.state
        self.state = self.CONNECTING
        self.connect_count += 1
        if self._has_connected or previous_state == self.BACKOFF:
            self.reconnect_count += 1

        try:
            ok = bool(self.client.connect())
        except (ModbusException, OSError) as exc:
            self._record_failure(f"connect failed: {exc}", now)
            self.logger.error(f"Modbus connect failed: {exc}")
            return False

        if not ok:
            self._record_failure("connect returned false", now)
            self.logger.error("Modbus connect failed")
            return False

        self.state = self.READY
        self.connected_since = now
        self.last_success_at = now
        self.last_error = None
        self.backoff_until = None
        self.consecutive_failures = 0
        self._has_connected = True
        return True

    def execute(
        self, operation_name: str, callback: Callable[[], Any]
    ) -> TransactionResult:
        if not self.ensure_connected():
            return TransactionResult(
                ok=False,
                skipped=True,
                error=self.last_error or "modbus connection unavailable",
            )

        try:
            value = callback()
        except (ModbusException, OSError) as exc:
            now = self.clock()
            self.transaction_failure_count += 1
            self.close(f"{operation_name} transport failure")
            self._record_failure(f"{operation_name} failed: {exc}", now)
            self.logger.error(f"Modbus {operation_name} failed: {exc}")
            return TransactionResult(
                ok=False,
                callback_started=True,
                error=str(exc),
            )

        now = self.clock()
        self.state = self.READY
        self.last_success_at = now
        self.last_error = None
        self.backoff_until = None
        self.consecutive_failures = 0
        return TransactionResult(ok=True, value=value, callback_started=True)

    def close(self, reason: str = "shutdown") -> None:
        if not self.client:
            self.state = self.DISCONNECTED
            self.connected_since = None
            return
        self.state = self.CLOSING
        try:
            self.client.close()
        except (ModbusException, OSError) as exc:
            self.logger.debug(f"Ignoring Modbus close error during {reason}: {exc}")
        finally:
            self.connected_since = None
            self.state = self.DISCONNECTED

    def diagnostics(self) -> dict:
        return {
            "modbus_connection_state": self.state,
            "modbus_connected": self.connected,
            "modbus_connected_since": self.connected_since,
            "modbus_last_success_at": self.last_success_at,
            "modbus_last_failure_at": self.last_failure_at,
            "modbus_last_error": self.last_error,
            "modbus_consecutive_failures": self.consecutive_failures,
            "modbus_backoff_until": self.backoff_until,
            "modbus_connect_count": self.connect_count,
            "modbus_reconnect_count": self.reconnect_count,
            "modbus_transaction_failure_count": self.transaction_failure_count,
        }

    def _record_failure(self, error: str, now: float) -> None:
        self.state = self.BACKOFF
        self.last_failure_at = now
        self.last_error = error
        self.consecutive_failures += 1
        delay = min(
            self.backoff_base * 2 ** (self.consecutive_failures - 1),
            self.backoff_max,
        )
        self.backoff_until = now + delay

    def _connection_expired(self, now: float) -> bool:
        return (
            self.max_connection_age is not None
            and self.connected_since is not None
            and now - self.connected_since >= self.max_connection_age
        )
