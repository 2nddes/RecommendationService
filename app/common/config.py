from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict


def _find_repo_root(start: Path) -> Path:
    cur = start
    while True:
        if (cur / "pyproject.toml").exists() or (cur / "requirements.txt").exists():
            return cur
        if cur.parent == cur:
            return start
        cur = cur.parent


def _default_config_path() -> Path:
    # app/common/config.py -> app/common -> app -> repo root
    repo_root = _find_repo_root(Path(__file__).resolve().parents[2])
    return repo_root / "config.json"


@lru_cache(maxsize=1)
def load_config() -> Dict[str, Any]:
    path = _default_config_path()
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def get(key: str, default: Any = None) -> Any:
    cfg = load_config()
    return cfg.get(key, default)


def get_str(key: str, default: str | None = None) -> str | None:
    v = get(key, default)
    if v is None:
        return None
    if isinstance(v, str):
        s = v.strip()
        return s if s != "" else default
    return str(v)


def get_int(key: str, default: int) -> int:
    v = get(key, default)
    try:
        return int(v)
    except Exception:
        return int(default)


def get_float(key: str, default: float) -> float:
    v = get(key, default)
    try:
        return float(v)
    except Exception:
        return float(default)


def get_bool(key: str, default: bool) -> bool:
    v = get(key, default)
    if isinstance(v, bool):
        return v
    if v is None:
        return default
    if isinstance(v, (int, float)):
        return bool(v)
    if isinstance(v, str):
        s = v.strip().lower()
        if s in {"1", "true", "yes", "on"}:
            return True
        if s in {"0", "false", "no", "off"}:
            return False
    return default
