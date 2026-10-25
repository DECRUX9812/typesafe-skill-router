"""Disk cache for System One responses so re-runs replay instead of paying for inference.

Keyed on a hash of the full request payload (state + model + questions), so tuning
thresholds, weights, or display code never re-asks the model for an answer that has not
changed. `--no-cache` bypasses both read and write; `--offline` reads only.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Optional

_PATH_LOCKS: dict[str, threading.RLock] = {}
_PATH_LOCKS_GUARD = threading.Lock()


def _lock_for(path: Path) -> threading.RLock:
    key = str(path.resolve())
    with _PATH_LOCKS_GUARD:
        lock = _PATH_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _PATH_LOCKS[key] = lock
        return lock


def request_key(payload: dict[str, Any]) -> str:
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


class JsonCache:
    """A flat {key: response} JSON store with hit/miss accounting."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._data: dict[str, Any] = {}
        # Shared per-path lock: parallel pre_llm_call hooks (e.g. subagent fan-out)
        # construct separate JsonCache instances pointing at the same file.
        self._lock = _lock_for(self.path)
        self.hits = 0
        self.misses = 0
        with self._lock:
            if self.path.exists():
                try:
                    loaded = json.loads(self.path.read_text(encoding="utf-8"))
                    if isinstance(loaded, dict):
                        self._data = loaded
                except (OSError, json.JSONDecodeError):
                    self._data = {}

    def __len__(self) -> int:
        return len(self._data)

    def get(self, payload: dict[str, Any]) -> Optional[dict[str, Any]]:
        with self._lock:
            hit = self._data.get(request_key(payload))
            if hit is None:
                self.misses += 1
                return None
            self.hits += 1
            return hit

    def put(self, payload: dict[str, Any], response: dict[str, Any]) -> None:
        with self._lock:
            self._data[request_key(payload)] = response
            self.save()

    def save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.exists():
                try:
                    on_disk = json.loads(self.path.read_text(encoding="utf-8"))
                    if isinstance(on_disk, dict):
                        on_disk.update(self._data)
                        self._data = on_disk
                except (OSError, json.JSONDecodeError):
                    pass
            tmp = self.path.with_name(
                f".{self.path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
            )
            try:
                tmp.write_text(
                    json.dumps(self._data, indent=1, sort_keys=True), encoding="utf-8"
                )
                for attempt in range(6):
                    try:
                        tmp.replace(self.path)
                        return
                    except PermissionError:
                        if attempt == 5:
                            return  # cache persistence is best-effort; never fail a turn
                        time.sleep(0.01 * (attempt + 1))
            except OSError:
                return
            finally:
                if tmp.exists():
                    try:
                        tmp.unlink()
                    except OSError:
                        pass
