from __future__ import annotations

import json
import os
import threading
from typing import Any, Dict


class ArtifactStore:
    """Persist small admin state (e.g., latest trained artifact paths) to disk."""

    def __init__(self, path: str = "data/admin_state.json") -> None:
        self._path = path
        self._lock = threading.RLock()

    def get_all(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._read())

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            data = self._read()
            return data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            data = self._read()
            data[key] = value
            self._write(data)

    def _read(self) -> Dict[str, Any]:
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                obj = json.load(f)
            if isinstance(obj, dict):
                return obj
            return {}
        except FileNotFoundError:
            return {}
        except Exception:
            return {}

    def _write(self, data: Dict[str, Any]) -> None:
        os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
        tmp = self._path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self._path)


_global_store: ArtifactStore | None = None


def get_artifact_store() -> ArtifactStore:
    global _global_store
    if _global_store is None:
        _global_store = ArtifactStore()
    return _global_store
