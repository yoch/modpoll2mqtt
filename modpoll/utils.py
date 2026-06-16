from __future__ import annotations

import datetime
import threading
from datetime import timezone

_thread_event = threading.Event()


def set_threading_event() -> None:
    _thread_event.set()


def clear_threading_event() -> None:
    _thread_event.clear()


def on_threading_event() -> bool:
    return _thread_event.is_set()


def delay_thread(timeout: float) -> None:
    _thread_event.wait(timeout=timeout)


def get_utc_time() -> float:
    return datetime.datetime.now(timezone.utc).timestamp()
