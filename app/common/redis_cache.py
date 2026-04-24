from __future__ import annotations

import json
import logging
from time import perf_counter
from typing import Iterable, Mapping, Sequence

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


def tag_recall_key(settings: Settings, *, tag_type: str, tag_id: int) -> str:
    return _build_key(settings, "recall", "tag", str(tag_type), int(tag_id))


def user_recommendation_list_key(settings: Settings, user_id: int) -> str:
    return _build_key(settings, "recommend", "user", int(user_id))


def user_recommendation_lock_key(settings: Settings, user_id: int) -> str:
    return _build_key(settings, "recommend", "user", int(user_id), "build_lock")


def search_result_key(settings: Settings, signature: str) -> str:
    return _build_key(settings, "search", str(signature))


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
    if int(end) < int(start):
        return []

    key = user_recommendation_list_key(settings, user_id)
    started = perf_counter()
    try:
        client = get_redis_client(settings)
        if client is None:
            return []
        rows = client.lrange(key, max(int(start), 0), max(int(end), 0))
        items = [int(raw) for raw in rows]
        logger.debug(
            "User recommendation page load, user_id=%s, key=%s, start=%s, end=%s, returned_count=%s, elapsed_ms=%.2f",
            user_id,
            key,
            start,
            end,
            len(items),
            (perf_counter() - started) * 1000.0,
        )
        return items
    except Exception:
        logger.exception(
            "User recommendation page load failed, user_id=%s, key=%s, start=%s, end=%s",
            user_id,
            key,
            start,
            end,
        )
        raise


def load_user_recommendation_total(settings: Settings, *, user_id: int) -> int:
    key = user_recommendation_list_key(settings, user_id)
    started = perf_counter()
    try:
        client = get_redis_client(settings)
        if client is None:
            return 0
        total = int(client.llen(key) or 0)
        logger.debug(
            "User recommendation total load, user_id=%s, key=%s, total=%s, elapsed_ms=%.2f",
            user_id,
            key,
            total,
            (perf_counter() - started) * 1000.0,
        )
        return total
    except Exception:
        logger.exception(
            "User recommendation total load failed, user_id=%s, key=%s",
            user_id,
            key,
        )
        raise


def pop_user_recommendation_items(settings: Settings, *, user_id: int, count: int) -> tuple[list[int], int]:
    if count <= 0:
        return [], load_user_recommendation_total(settings, user_id=user_id)

    key = user_recommendation_list_key(settings, user_id)
    try:
        client = get_redis_client(settings)
        if client is None:
            logger.warning("No Redis client during recommendation pop, user_id=%s, key=%s, count=%s", user_id, key, count)
            return [], 0
        pipe = client.pipeline(transaction=True)
        pipe.lrange(key, 0, count - 1)
        pipe.ltrim(key, count, -1)
        pipe.llen(key)
        rows, _trim_result, remaining = pipe.execute()
        items = [int(raw) for raw in rows]
        remaining_count = int(remaining or 0)
        logger.debug(
            "User recommendation pop, user_id=%s, key=%s, count=%s, returned_count=%s, remaining=%s, item_preview=%s",
            user_id,
            key,
            count,
            len(items),
            remaining_count,
            items[:5],
        )
        return items, remaining_count
    except Exception:
        logger.exception(
            "User recommendation pop failed during redis, user_id=%s, key=%s, count=%s",
            user_id,
            key,
            count,
        )
        raise


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


def load_search_result(settings: Settings, *, signature: str) -> dict | None:
    client = get_redis_client(settings)
    if client is None:
        return None

    key = search_result_key(settings, signature)
    raw = client.get(key)
    if not raw:
        return None

    loaded = json.loads(raw)
    if isinstance(loaded, dict):
        return loaded
    return None


def store_search_result(settings: Settings, *, signature: str, payload: Mapping[str, object]) -> bool:
    client = get_redis_client(settings)
    if client is None:
        return False

    ttl = max(int(settings.cache.search_cache_ttl_seconds), 1)
    key = search_result_key(settings, signature)
    client.set(key, json.dumps(dict(payload), ensure_ascii=False), ex=ttl)
    return True


def store_tag_recall_items(
    settings: Settings,
    *,
    tag_items: Mapping[tuple[str, int], Sequence[tuple[int, float]]],
    retain_topn: int,
) -> dict[str, int]:
    client = get_redis_client(settings)
    if client is None:
        return {}

    retain_limit = max(int(retain_topn), 0)
    written: dict[str, int] = {}
    pipe = client.pipeline(transaction=False)

    for (tag_type_raw, tag_id_raw), pairs in tag_items.items():
        tag_type = str(tag_type_raw).strip().lower()
        if not tag_type:
            continue

        tag_id = int(tag_id_raw)
        key = tag_recall_key(settings, tag_type=tag_type, tag_id=tag_id)

        payload: dict[str, float] = {}
        for item_id_raw, score_raw in pairs:
            item_id = int(item_id_raw)
            score = float(score_raw)
            if item_id <= 0 or score <= 0.0:
                continue
            member = str(item_id)
            prev = payload.get(member)
            if prev is None or score > prev:
                payload[member] = score

        if not payload:
            continue

        pipe.delete(key)
        pipe.zadd(key, payload)
        if retain_limit > 0:
            pipe.zremrangebyrank(key, 0, -(retain_limit + 1))
        written[key] = len(payload)

    if written:
        pipe.execute()
    return written


def load_tag_recall_items(
    settings: Settings,
    *,
    tag_specs: Sequence[tuple[str, int]],
    per_tag_topn: int,
) -> dict[tuple[str, int], list[tuple[int, float]]]:
    client = get_redis_client(settings)
    topn = max(int(per_tag_topn), 0)
    if client is None or topn <= 0 or not tag_specs:
        return {}

    normalized_specs: list[tuple[str, int]] = []
    pipe = client.pipeline(transaction=False)
    for tag_type_raw, tag_id_raw in tag_specs:
        tag_type = str(tag_type_raw).strip().lower()
        if not tag_type:
            continue
        spec = (tag_type, int(tag_id_raw))
        normalized_specs.append(spec)
        key = tag_recall_key(settings, tag_type=spec[0], tag_id=spec[1])
        pipe.zrevrange(key, 0, topn - 1, withscores=True)

    if not normalized_specs:
        return {}

    rows_by_spec: dict[tuple[str, int], list[tuple[int, float]]] = {}
    fetched = pipe.execute()
    for spec, rows in zip(normalized_specs, fetched):
        parsed: list[tuple[int, float]] = []
        for movie_id_raw, score_raw in rows:
            parsed.append((int(movie_id_raw), float(score_raw)))
        rows_by_spec[spec] = parsed

    return rows_by_spec


def store_user_recommendation_items(settings: Settings, *, user_id: int, items: Sequence[int]) -> int:
    normalized = [int(item_id) for item_id in items]
    key = user_recommendation_list_key(settings, user_id)
    ttl = max(int(settings.cache.user_reco_ttl_seconds), 60)
    started = perf_counter()
    try:
        client = get_redis_client(settings)
        if client is None:
            return 0
        pipe = client.pipeline(transaction=True)
        pipe.delete(key)
        if normalized:
            pipe.rpush(key, *[str(item_id) for item_id in normalized])
            pipe.expire(key, ttl)
        pipe.execute()
        logger.info(
            "User recommendation store, user_id=%s, key=%s, stored_count=%s, ttl=%s, item_preview=%s, elapsed_ms=%.2f",
            user_id,
            key,
            len(normalized),
            ttl,
            normalized[:5],
            (perf_counter() - started) * 1000.0,
        )
        return len(normalized)
    except Exception:
        logger.exception(
            "User recommendation store failed, user_id=%s, key=%s, item_count=%s, ttl=%s",
            user_id,
            key,
            len(normalized),
            ttl,
        )
        raise


def try_acquire_user_recommendation_lock(
    settings: Settings,
    *,
    user_id: int,
    token: str,
    ttl_seconds: int | None = None,
) -> bool:
    key = user_recommendation_lock_key(settings, user_id)
    ttl = int(ttl_seconds if ttl_seconds is not None else settings.cache.user_reco_build_lock_seconds)
    started = perf_counter()
    token_tail = str(token or "")[-8:]
    try:
        client = get_redis_client(settings)
        if client is None:
            return False
        acquired = bool(client.set(key, token, nx=True, ex=max(ttl, 1)))
        logger.log(
            logging.INFO if acquired else logging.DEBUG,
            "User recommendation lock acquire, user_id=%s, key=%s, ttl=%s, acquired=%s, token_tail=%s, elapsed_ms=%.2f",
            user_id,
            key,
            ttl,
            acquired,
            token_tail,
            (perf_counter() - started) * 1000.0,
        )
        return acquired
    except Exception:
        logger.exception(
            "User recommendation lock acquire failed, user_id=%s, key=%s, ttl=%s",
            user_id,
            key,
            ttl,
        )
        raise


def release_user_recommendation_lock(settings: Settings, *, user_id: int, token: str) -> None:
    if not token:
        return

    key = user_recommendation_lock_key(settings, user_id)
    started = perf_counter()
    token_tail = str(token or "")[-8:]
    try:
        client = get_redis_client(settings)
        if client is None:
            return
        released = int(
            client.eval(
                "if redis.call('GET', KEYS[1]) == ARGV[1] then "
                "return redis.call('DEL', KEYS[1]) else return 0 end",
                1,
                key,
                token,
            )
            or 0
        )
        logger.info(
            "User recommendation lock release, user_id=%s, key=%s, released=%s, token_tail=%s, elapsed_ms=%.2f",
            user_id,
            key,
            bool(released),
            token_tail,
            (perf_counter() - started) * 1000.0,
        )
    except Exception:
        logger.exception(
            "User recommendation lock release failed, user_id=%s, key=%s",
            user_id,
            key,
        )
        raise


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
