from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
import logging
import threading
from typing import Any, Callable, Dict, Optional


logger = logging.getLogger(__name__)


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
            logger.info("创建后台任务，task_id=%s, name=%s", task_id, name)
            return t

    def get(self, task_id: str) -> Task | None:
        with self._lock:
            return self._tasks.get(task_id)

    def list(self) -> list[Task]:
        with self._lock:
            return list(self._tasks.values())

    def start(self, *, task_id: str, name: str, fn: TaskFn) -> Task:
        t = self.create(task_id=task_id, name=name)
        logger.info("准备启动后台任务，task_id=%s, name=%s", task_id, name)

        def _runner() -> None:
            with self._lock:
                t.status = "running"
                t.started_at = _now_iso()
            logger.info("后台任务开始执行，task_id=%s", task_id)

            result = fn() or {}
            with self._lock:
                t.result = dict(result)
                t.status = "succeeded"
                t.finished_at = _now_iso()
            logger.info("后台任务执行成功，task_id=%s", task_id)

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
