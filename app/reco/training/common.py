from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass
import json
import logging
import random
from typing import Any, Dict, List, Sequence

from sqlalchemy import Engine, create_engine


_RANDOM_SPLIT_SEED = 20260404


@dataclass(frozen=True)
class TrainOutcome:
    component: str
    name: str
    artifact_path: str | None
    trained: bool
    details: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def to_train_outcome(payload: Dict[str, Any]) -> TrainOutcome:
    return TrainOutcome(
        component=str(payload.get("component") or ""),
        name=str(payload.get("name") or ""),
        artifact_path=payload.get("artifact_path"),
        trained=bool(payload.get("trained")),
        details=payload.get("details") if isinstance(payload.get("details"), dict) else {},
    )


def fmt_log_value(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float, str)):
        return str(value)
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except Exception:
        return str(value)


def log_event(logger: logging.Logger, level: str, event: str, **fields: Any) -> None:
    payload: Dict[str, Any] = {"event": event}
    payload.update(fields)
    message = " | ".join(f"{k}={fmt_log_value(v)}" for k, v in payload.items())
    getattr(logger, level, logger.info)(message)


def log_exception(logger: logging.Logger, event: str, error: Exception, **fields: Any) -> None:
    payload: Dict[str, Any] = {"event": event, "error": f"{type(error).__name__}: {error}"}
    payload.update(fields)
    message = " | ".join(f"{k}={fmt_log_value(v)}" for k, v in payload.items())
    logger.exception(message)


@contextmanager
def catch_and_reraise(
    logger: logging.Logger,
    event: str,
    error_prefix: str,
    **fields: Any,
):
    try:
        yield
    except Exception as e:  # noqa: BLE001
        log_exception(logger, event, e, **fields)
        raise RuntimeError(f"{error_prefix}: {type(e).__name__}: {e}") from e


def get_mysql_engine(mysql_dsn: str | None, *, logger: logging.Logger, event_prefix: str = "mysql.engine") -> Engine:
    dsn = mysql_dsn
    if not dsn:
        err = RuntimeError("mysql_dsn_missing")
        log_exception(logger, f"{event_prefix}.dsn_missing", err)
        raise err
    try:
        return create_engine(dsn, pool_pre_ping=True)
    except Exception as e:
        log_exception(logger, f"{event_prefix}.create_failed", e, mysql_dsn_set=bool(dsn.strip()))
        raise RuntimeError(f"mysql_engine_create_failed: {type(e).__name__}: {e}") from e


def binary_auc(y_true: Sequence[float], y_score: Sequence[float]) -> float:
    if len(y_true) != len(y_score) or len(y_true) == 0:
        raise ValueError("invalid_auc_input")

    pairs = [(float(y), float(s)) for y, s in zip(y_true, y_score)]
    pos_count = sum(1 for y, _ in pairs if y > 0.5)
    neg_count = len(pairs) - pos_count
    if pos_count == 0 or neg_count == 0:
        raise ValueError("auc_requires_both_classes")

    pairs.sort(key=lambda x: x[1])

    rank_sum_pos = 0.0
    i = 0
    n = len(pairs)
    while i < n:
        j = i + 1
        while j < n and pairs[j][1] == pairs[i][1]:
            j += 1
        avg_rank = ((i + 1) + j) / 2.0
        pos_in_group = sum(1 for k in range(i, j) if pairs[k][0] > 0.5)
        rank_sum_pos += avg_rank * pos_in_group
        i = j

    return (rank_sum_pos - (pos_count * (pos_count + 1) / 2.0)) / (pos_count * neg_count)


def simple_train_test_split_indices(total: int, train_ratio: float = 0.8) -> tuple[List[int], List[int]]:
    n = total
    if n <= 1:
        raise ValueError("split_requires_more_rows")
    shuffled_idx = list(range(n))
    random.Random(_RANDOM_SPLIT_SEED + n).shuffle(shuffled_idx)
    cut = int(n * train_ratio)
    cut = max(1, min(n - 1, cut))
    train_idx = shuffled_idx[:cut]
    test_idx = shuffled_idx[cut:]
    if not train_idx or not test_idx:
        raise ValueError("split_result_empty")
    return train_idx, test_idx


def binary_train_test_split_indices(y: Sequence[float], train_ratio: float = 0.8) -> tuple[List[int], List[int]]:
    pos = [i for i, v in enumerate(y) if v > 0.5]
    neg = [i for i, v in enumerate(y) if v <= 0.5]
    if not pos or not neg:
        raise ValueError("split_requires_both_classes")

    def _split(group: List[int]) -> tuple[List[int], List[int]]:
        if len(group) <= 1:
            return group[:], []
        shuffled = group[:]
        random.Random(_RANDOM_SPLIT_SEED + len(shuffled)).shuffle(shuffled)
        cut = int(len(shuffled) * train_ratio)
        cut = max(1, min(len(shuffled) - 1, cut))
        return shuffled[:cut], shuffled[cut:]

    pos_train, pos_test = _split(pos)
    neg_train, neg_test = _split(neg)

    train_idx = pos_train + neg_train
    test_idx = pos_test + neg_test
    if not train_idx or not test_idx:
        raise ValueError("split_result_empty")
    return train_idx, test_idx


def group_train_test_split_indices(groups: Sequence[int], train_ratio: float = 0.8) -> tuple[List[int], List[int]]:
    uniq = sorted({int(g) for g in groups})
    if len(uniq) <= 1:
        raise ValueError("group_split_requires_more_groups")

    shuffled_groups = uniq[:]
    random.Random(_RANDOM_SPLIT_SEED + len(shuffled_groups)).shuffle(shuffled_groups)
    cut = int(len(shuffled_groups) * train_ratio)
    cut = max(1, min(len(shuffled_groups) - 1, cut))

    train_groups = set(shuffled_groups[:cut])
    train_idx = [i for i, g in enumerate(groups) if int(g) in train_groups]
    test_idx = [i for i, g in enumerate(groups) if int(g) not in train_groups]
    if not train_idx or not test_idx:
        raise ValueError("group_split_result_empty")
    return train_idx, test_idx
