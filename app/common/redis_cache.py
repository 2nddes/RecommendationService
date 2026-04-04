from __future__ import annotations

import json
import logging
from typing import Iterable, Sequence

from app.common.settings import Settings


logger = logging.getLogger(__name__)

_client = None
_client_key: tuple[str, int, int, str | None, bool] | None = None


def _build_key(settings: Settings, *parts: object) -> str:
    prefix = settings.cache.key_prefix.strip() if settings.cache.key_prefix else "reco"
    tail = ":".join(str(x) for x in parts)
    return f"{prefix}:{tail}" if tail else prefix


def _redis_import():
    try:
        import redis  # type: ignore

        return redis
    except Exception:
        return None


def get_redis_client(settings: Settings):
    global _client, _client_key

    if not settings.redis.enabled:
        return None

    redis_mod = _redis_import()
    if redis_mod is None:
        logger.warning("Redis is enabled in config but package 'redis' is not installed")
        return None

    identity = (
        settings.redis.host,
        int(settings.redis.port),
        int(settings.redis.db),
        settings.redis.username,
        bool(settings.redis.ssl),
    )
    if _client is not None and _client_key == identity:
        return _client

    try:
        client = redis_mod.Redis(
            host=settings.redis.host,
            port=int(settings.redis.port),
            db=int(settings.redis.db),
            username=settings.redis.username,
            password=settings.redis.password,
            ssl=bool(settings.redis.ssl),
            socket_timeout=float(settings.redis.socket_timeout_s),
            socket_connect_timeout=float(settings.redis.connect_timeout_s),
            decode_responses=True,
        )
        client.ping()
        _client = client
        _client_key = identity
        return _client
    except Exception:
        logger.exception("Redis connection failed")
        _client = None
        _client_key = None
        return None


def movie_feature_key(settings: Settings, movie_id: int) -> str:
    return _build_key(settings, "feature", "movie", int(movie_id))


def user_feature_key(settings: Settings, user_id: int) -> str:
    return _build_key(settings, "feature", "user", int(user_id))


def trending_key(settings: Settings, window: str) -> str:
    return _build_key(settings, "trending", str(window))


def item_similar_key(settings: Settings, movie_id: int) -> str:
    return _build_key(settings, "recall", "item_similar_by_tags", int(movie_id))


def user_interest_key(settings: Settings, user_id: int) -> str:
    return _build_key(settings, "recall", "user_interest_tag", int(user_id))


def load_trending_items(settings: Settings, *, window: str, n: int) -> list[int]:
    client = get_redis_client(settings)
    if client is None:
        return []

    key = trending_key(settings, window)
    try:
        rows = client.zrevrange(key, 0, max(int(n) - 1, 0))
    except Exception:
        logger.exception("Redis read failed for trending key=%s", key)
        return []

    out: list[int] = []
    for raw in rows:
        try:
            out.append(int(raw))
        except Exception:
            continue
    return out


def store_trending_items(settings: Settings, *, window: str, pairs: Sequence[tuple[int, float]]) -> int:
    client = get_redis_client(settings)
    if client is None:
        return 0

    key = trending_key(settings, window)
    payload = {str(int(item_id)): float(score) for item_id, score in pairs if int(item_id) > 0}
    if not payload:
        return 0

    try:
        pipe = client.pipeline(transaction=False)
        pipe.delete(key)
        pipe.zadd(key, payload)
        pipe.expire(key, max(int(settings.cache.trending_refresh_interval_seconds) * 3, 300))
        pipe.execute()
        return len(payload)
    except Exception:
        logger.exception("Redis write failed for trending key=%s", key)
        return 0


def load_item_similar(settings: Settings, *, movie_id: int, n: int) -> list[int]:
    client = get_redis_client(settings)
    if client is None:
        return []

    key = item_similar_key(settings, movie_id)
    try:
        rows = client.zrevrange(key, 0, max(int(n) - 1, 0))
    except Exception:
        logger.exception("Redis read failed for item-similar key=%s", key)
        return []

    out: list[int] = []
    for raw in rows:
        try:
            out.append(int(raw))
        except Exception:
            continue
    return out


def load_item_similar_candidates(settings: Settings, *, movie_id: int, topk: int, source: str):
    from app.reco.types import Candidate

    client = get_redis_client(settings)
    if client is None:
        return []

    key = item_similar_key(settings, movie_id)
    try:
        rows = client.zrevrange(key, 0, max(int(topk) - 1, 0), withscores=True)
    except Exception:
        logger.exception("Redis read failed for item-similar candidates key=%s", key)
        return []

    out = []
    for raw_item, score in rows:
        try:
            out.append(Candidate(item_id=int(raw_item), score=float(score), source=source))
        except Exception:
            continue
    return out


def store_item_similar(settings: Settings, *, movie_id: int, pairs: Sequence[tuple[int, float]]) -> int:
    client = get_redis_client(settings)
    if client is None:
        return 0

    key = item_similar_key(settings, movie_id)
    payload = {str(int(item_id)): float(score) for item_id, score in pairs if int(item_id) > 0 and int(item_id) != int(movie_id)}
    if not payload:
        return 0

    try:
        pipe = client.pipeline(transaction=False)
        pipe.delete(key)
        pipe.zadd(key, payload)
        pipe.expire(key, max(int(settings.cache.recall_ttl_seconds), 300))
        pipe.execute()
        return len(payload)
    except Exception:
        logger.exception("Redis write failed for item-similar key=%s", key)
        return 0


def load_user_interest_candidates(settings: Settings, *, user_id: int, topk: int, source: str):
    from app.reco.types import Candidate

    client = get_redis_client(settings)
    if client is None:
        return []

    key = user_interest_key(settings, user_id)
    try:
        rows = client.zrevrange(key, 0, max(int(topk) - 1, 0), withscores=True)
    except Exception:
        logger.exception("Redis read failed for user-interest key=%s", key)
        return []

    out = []
    for raw_item, score in rows:
        try:
            out.append(Candidate(item_id=int(raw_item), score=float(score), source=source))
        except Exception:
            continue
    return out


def store_user_interest(settings: Settings, *, user_id: int, pairs: Sequence[tuple[int, float]]) -> int:
    client = get_redis_client(settings)
    if client is None:
        return 0

    key = user_interest_key(settings, user_id)
    payload = {str(int(item_id)): float(score) for item_id, score in pairs if int(item_id) > 0}
    if not payload:
        return 0

    try:
        pipe = client.pipeline(transaction=False)
        pipe.delete(key)
        pipe.zadd(key, payload)
        pipe.expire(key, max(int(settings.cache.recall_ttl_seconds), 300))
        pipe.execute()
        return len(payload)
    except Exception:
        logger.exception("Redis write failed for user-interest key=%s", key)
        return 0


def load_movie_feature_hash(settings: Settings, movie_id: int) -> dict[str, str]:
    client = get_redis_client(settings)
    if client is None:
        return {}
    key = movie_feature_key(settings, movie_id)
    try:
        raw = client.hgetall(key) or {}
    except Exception:
        logger.exception("Redis read failed for movie feature key=%s", key)
        return {}
    return {str(k): str(v) for k, v in raw.items()}


def load_user_feature_hash(settings: Settings, user_id: int) -> dict[str, str]:
    client = get_redis_client(settings)
    if client is None:
        return {}
    key = user_feature_key(settings, user_id)
    try:
        raw = client.hgetall(key) or {}
    except Exception:
        logger.exception("Redis read failed for user feature key=%s", key)
        return {}
    return {str(k): str(v) for k, v in raw.items()}


def store_movie_features(settings: Settings, rows: Iterable[dict]) -> int:
    client = get_redis_client(settings)
    if client is None:
        return 0

    ttl = max(int(settings.cache.feature_ttl_seconds), 300)
    count = 0
    try:
        pipe = client.pipeline(transaction=False)
        for row in rows:
            try:
                movie_id = int(row.get("movie_id") or 0)
            except Exception:
                continue
            if movie_id <= 0:
                continue
            key = movie_feature_key(settings, movie_id)
            payload = {
                "rating_avg": str(row.get("rating_avg") or "0"),
                "year": str(row.get("year") or "0"),
                "duration_min": str(row.get("duration_min") or "0"),
                "tags": json.dumps(row.get("tags") or [], ensure_ascii=False),
            }
            pipe.hset(key, mapping=payload)
            pipe.expire(key, ttl)
            count += 1
        if count > 0:
            pipe.execute()
        return count
    except Exception:
        logger.exception("Redis write failed for movie feature hashes")
        return 0


def store_user_features(settings: Settings, rows: Iterable[dict]) -> int:
    client = get_redis_client(settings)
    if client is None:
        return 0

    ttl = max(int(settings.cache.feature_ttl_seconds), 300)
    count = 0
    try:
        pipe = client.pipeline(transaction=False)
        for row in rows:
            try:
                user_id = int(row.get("user_id") or 0)
            except Exception:
                continue
            if user_id <= 0:
                continue
            key = user_feature_key(settings, user_id)
            payload = {
                "gender": str(row.get("gender") or ""),
                "birth": str(row.get("birth") or ""),
                "created_at": str(row.get("created_at") or ""),
                "interest_tags": json.dumps(row.get("interest_tags") or [], ensure_ascii=False),
            }
            pipe.hset(key, mapping=payload)
            pipe.expire(key, ttl)
            count += 1
        if count > 0:
            pipe.execute()
        return count
    except Exception:
        logger.exception("Redis write failed for user feature hashes")
        return 0
