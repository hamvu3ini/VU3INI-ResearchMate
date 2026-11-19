"""Cached online/offline detection for the header status pill."""
from __future__ import annotations

import os
import socket
import threading
import time

_PROBES = [("api.crossref.org", 443), ("api.openalex.org", 443), ("1.1.1.1", 443)]
_cache = {"t": 0.0, "online": False}
_lock = threading.Lock()


def is_online(max_age: float = 20.0) -> bool:
    forced = os.environ.get("RESEARCHMATE_FORCE_OFFLINE")
    if forced == "1":
        return False
    with _lock:
        if time.time() - _cache["t"] < max_age:
            return _cache["online"]
    online = False
    for host, port in _PROBES:
        try:
            with socket.create_connection((host, port), timeout=1.5):
                online = True
                break
        except OSError:
            continue
    with _lock:
        _cache.update(t=time.time(), online=online)
    return online
