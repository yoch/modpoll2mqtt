"""Shared Modbus TCP helpers for integration tests and the optional standalone server."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pymodbus.datastore import (
    ModbusDeviceContext,
    ModbusSequentialDataBlock,
    ModbusServerContext,
)
from pymodbus.server import StartTcpServer

_SERVER_MODULE = Path(__file__).resolve()


@dataclass(frozen=True)
class ModbusEndpoint:
    host: str
    port: int


def get_modbus_test_endpoint() -> ModbusEndpoint:
    host = os.environ.get("MODBUS_TEST_HOST", "127.0.0.1")
    port = int(os.environ.get("MODBUS_TEST_PORT", "1502"))
    return ModbusEndpoint(host=host, port=port)


def tcp_reachable(host: str, port: int, timeout: float = 3.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _run_modbus_server(host: str, port: int) -> None:
    block = ModbusSequentialDataBlock(1, [0] * 50000)
    device = ModbusDeviceContext(di=block, co=block, hr=block, ir=block)
    context = ModbusServerContext(devices=device, single=True)
    StartTcpServer(context=context, address=(host, port))


def start_modbus_test_server_process(
    endpoint: ModbusEndpoint,
    *,
    wait_s: float = 5.0,
) -> subprocess.Popen[Any]:
    """Start this module as a server process; return it once the port is reachable."""
    env = {
        **os.environ,
        "MODBUS_TEST_HOST": endpoint.host,
        "MODBUS_TEST_PORT": str(endpoint.port),
    }
    proc = subprocess.Popen(
        [sys.executable, str(_SERVER_MODULE)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            output = proc.stdout.read() if proc.stdout else ""
            raise RuntimeError(
                f"modbus test server exited with code {proc.returncode} "
                f"before becoming reachable at {endpoint.host}:{endpoint.port}\n{output}"
            )
        if tcp_reachable(endpoint.host, endpoint.port):
            return proc
        time.sleep(0.1)

    proc.terminate()
    proc.wait(timeout=3)
    raise RuntimeError(
        f"modbus test server did not become reachable at {endpoint.host}:{endpoint.port} "
        f"within {wait_s}s"
    )


def stop_modbus_test_server_process(proc: subprocess.Popen[Any]) -> None:
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def ensure_modbus_test_server(
    endpoint: ModbusEndpoint,
    *,
    wait_s: float = 5.0,
) -> subprocess.Popen[Any] | None:
    """Return None when a server already listens; otherwise start one."""
    if tcp_reachable(endpoint.host, endpoint.port):
        return None
    return start_modbus_test_server_process(endpoint, wait_s=wait_s)


def run_modbus_test_server() -> None:
    """Run a blocking Modbus TCP test server (for manual use)."""
    endpoint = get_modbus_test_endpoint()
    print(
        f"Modbus TCP test server listening on {endpoint.host}:{endpoint.port}",
        flush=True,
    )
    _run_modbus_server(endpoint.host, endpoint.port)


if __name__ == "__main__":
    run_modbus_test_server()
