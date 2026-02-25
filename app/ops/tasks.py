from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
import threading
import traceback
from typing import Any, Callable, Dict, Optional


@dataclass
class Task:
    id: str
    name: str
    status: str  # pending|running|succeeded|failed
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None
    error: str | None = None
    result: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


TaskFn = Callable[[], Dict[str, Any]]


class TaskManager:
    """A tiny in-process background task runner.

    Notes:
    - This is intentionally simple: thread-based, in-memory.
    - Suitable for dev / small deployments. For production, replace with Celery/RQ/etc.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._tasks: Dict[str, Task] = {}

    def create(self, *, task_id: str, name: str) -> Task:
        with self._lock:
            t = Task(id=task_id, name=name, status="pending", created_at=_now_iso())
            self._tasks[task_id] = t
            return t

    def get(self, task_id: str) -> Task | None:
        with self._lock:
            return self._tasks.get(task_id)

    def list(self) -> list[Task]:
        with self._lock:
            return list(self._tasks.values())

    def start(self, *, task_id: str, name: str, fn: TaskFn) -> Task:
        t = self.create(task_id=task_id, name=name)

        def _runner() -> None:
            with self._lock:
                t.status = "running"
                t.started_at = _now_iso()

            try:
                result = fn() or {}
                with self._lock:
                    t.result = dict(result)
                    t.status = "succeeded"
                    t.finished_at = _now_iso()
            except Exception as e:  # noqa: BLE001
                err = f"{type(e).__name__}: {e}"
                tb = traceback.format_exc(limit=50)
                with self._lock:
                    t.status = "failed"
                    t.error = err + "\n" + tb
                    t.finished_at = _now_iso()

        th = threading.Thread(target=_runner, name=f"task:{task_id}", daemon=True)
        th.start()
        return t


_global_task_manager: TaskManager | None = None


def get_task_manager() -> TaskManager:
    global _global_task_manager
    if _global_task_manager is None:
        _global_task_manager = TaskManager()
    return _global_task_manager


def _now_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
