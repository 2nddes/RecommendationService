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
    import redis  # type: ignore

    return redis


def get_redis_client(settings: Settings):
    global _client, _client_key

    if not settings.redis.enabled:
        return None

    redis_mod = _redis_import()

    identity = (
        settings.redis.host,
        int(settings.redis.port),
        int(settings.redis.db),
        settings.redis.username,
        bool(settings.redis.ssl),
    )
    if _client is not None and _client_key == identity:
        return _client

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


def movie_feature_key(settings: Settings, movie_id: int) -> str:
    return _build_key(settings, "feature", "movie", int(movie_id))


def user_feature_key(settings: Settings, user_id: int) -> str:
    return _build_key(settings, "feature", "user", int(user_id))


def trending_key(settings: Settings, window: str) -> str:
    return _build_key(settings, "trending", str(window))


def user_recommendation_list_key(settings: Settings, user_id: int) -> str:
    return _build_key(settings, "recommend", "user", int(user_id))


def user_recommendation_lock_key(settings: Settings, user_id: int) -> str:
    return _build_key(settings, "recommend", "user", int(user_id), "build_lock")


def load_trending_items(settings: Settings, *, window: str, n: int) -> list[int]:
    client = get_redis_client(settings)
    if client is None:
        return []

    key = trending_key(settings, window)
    rows = client.zrevrange(key, 0, max(int(n) - 1, 0))

    out: list[int] = []
    for raw in rows:
        out.append(int(raw))
    return out


def load_user_recommendation_page(
    settings: Settings,
    *,
    user_id: int,
    start: int,
    end: int,
) -> list[int]:
    client = get_redis_client(settings)
    if client is None:
        return []
    if int(end) < int(start):
        return []

    key = user_recommendation_list_key(settings, user_id)
    rows = client.lrange(key, max(int(start), 0), max(int(end), 0))
    return [int(raw) for raw in rows]


def load_user_recommendation_total(settings: Settings, *, user_id: int) -> int:
    client = get_redis_client(settings)
    if client is None:
        return 0
    key = user_recommendation_list_key(settings, user_id)
    return int(client.llen(key) or 0)


def pop_user_recommendation_items(settings: Settings, *, user_id: int, count: int) -> tuple[list[int], int]:
    client = get_redis_client(settings)
    if client is None:
        return [], 0

    normalized = max(int(count), 0)
    if normalized <= 0:
        return [], load_user_recommendation_total(settings, user_id=user_id)

    key = user_recommendation_list_key(settings, user_id)
    pipe = client.pipeline(transaction=True)
    pipe.lrange(key, 0, normalized - 1)
    pipe.ltrim(key, normalized, -1)
    pipe.llen(key)
    rows, _trim_result, remaining = pipe.execute()
    return [int(raw) for raw in rows], int(remaining or 0)


def store_trending_items(settings: Settings, *, window: str, pairs: Sequence[tuple[int, float]]) -> int:
    client = get_redis_client(settings)
    if client is None:
        return 0

    key = trending_key(settings, window)
    payload = {str(int(item_id)): float(score) for item_id, score in pairs if int(item_id) > 0}
    if not payload:
        return 0

    pipe = client.pipeline(transaction=False)
    pipe.delete(key)
    pipe.zadd(key, payload)
    pipe.expire(key, max(int(settings.cache.trending_refresh_interval_seconds) * 3, 300))
    pipe.execute()
    return len(payload)


def store_user_recommendation_items(settings: Settings, *, user_id: int, items: Sequence[int]) -> int:
    client = get_redis_client(settings)
    if client is None:
        return 0

    normalized = [int(item_id) for item_id in items]
    key = user_recommendation_list_key(settings, user_id)
    ttl = max(int(settings.cache.user_reco_ttl_seconds), 60)

    pipe = client.pipeline(transaction=False)
    pipe.delete(key)
    if normalized:
        pipe.rpush(key, *[str(item_id) for item_id in normalized])
        pipe.expire(key, ttl)
    pipe.execute()
    return len(normalized)


def try_acquire_user_recommendation_lock(
    settings: Settings,
    *,
    user_id: int,
    token: str,
    ttl_seconds: int | None = None,
) -> bool:
    client = get_redis_client(settings)
    if client is None:
        return False

    key = user_recommendation_lock_key(settings, user_id)
    ttl = int(ttl_seconds if ttl_seconds is not None else settings.cache.user_reco_build_lock_seconds)
    return bool(client.set(key, token, nx=True, ex=max(ttl, 1)))


def release_user_recommendation_lock(settings: Settings, *, user_id: int, token: str) -> None:
    client = get_redis_client(settings)
    if client is None or not token:
        return

    key = user_recommendation_lock_key(settings, user_id)
    client.eval(
        "if redis.call('GET', KEYS[1]) == ARGV[1] then "
        "return redis.call('DEL', KEYS[1]) else return 0 end",
        1,
        key,
        token,
    )


def load_movie_feature_hash(settings: Settings, movie_id: int) -> dict[str, str]:
    client = get_redis_client(settings)
    if client is None:
        return {}
    key = movie_feature_key(settings, movie_id)
    raw = client.hgetall(key) or {}
    return {str(k): str(v) for k, v in raw.items()}


def load_user_feature_hash(settings: Settings, user_id: int) -> dict[str, str]:
    client = get_redis_client(settings)
    if client is None:
        return {}
    key = user_feature_key(settings, user_id)
    raw = client.hgetall(key) or {}
    return {str(k): str(v) for k, v in raw.items()}


def store_movie_features(settings: Settings, rows: Iterable[dict]) -> int:
    client = get_redis_client(settings)
    if client is None:
        return 0

    ttl = max(int(settings.cache.feature_ttl_seconds), 300)
    count = 0
    pipe = client.pipeline(transaction=False)
    for row in rows:
        movie_id = int(row["movie_id"])
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


def store_user_features(settings: Settings, rows: Iterable[dict]) -> int:
    client = get_redis_client(settings)
    if client is None:
        return 0

    ttl = max(int(settings.cache.feature_ttl_seconds), 300)
    count = 0
    pipe = client.pipeline(transaction=False)
    for row in rows:
        user_id = int(row["user_id"])
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
