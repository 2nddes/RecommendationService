from __future__ import annotations

from typing import List


def bundle_feature_order() -> List[str]:
    return [
        "recall_score",
        "movie_rating_avg",
        "movie_rating_count",
        "movie_comment_count",
        "movie_click_count",
        "movie_click_1h",
        "movie_click_24h",
        "movie_year",
        "movie_duration_min",
        "user_static_tag_ctr",
        "src_user_collection",
        "src_user_high_rating_similar",
        "src_user_interest_tag",
        "src_item_similar_by_tags",
        "src_two_tower",
    ]

PAD_IDX = 0
UNK_IDX = 1

SHORT_INTEREST_SEQ_LEN = 10
LONG_INTEREST_TAG_SEQ_LEN = 100
ITEM_TAG_SEQ_LEN = 12


def normalize_gender(gender: str | None) -> str:
    g = str(gender or "unknown").strip().lower()
    if g in {"male", "female"}:
        return g
    return "unknown"


def bucketize_age(age: int | None) -> str:
    if age is None or age <= 0:
        return "unknown"
    if age < 18:
        return "lt18"
    if age < 25:
        return "18_24"
    if age < 35:
        return "25_34"
    if age < 45:
        return "35_44"
    if age < 55:
        return "45_54"
    return "55_plus"


def default_gender_index() -> dict[str, int]:
    return {"unknown": 1, "male": 2, "female": 3}


def default_age_bucket_index() -> dict[str, int]:
    return {
        "unknown": 1,
        "lt18": 2,
        "18_24": 3,
        "25_34": 4,
        "35_44": 5,
        "45_54": 6,
        "55_plus": 7,
    }


def pad_or_truncate(seq: List[int], *, size: int, pad_value: int = PAD_IDX) -> List[int]:
    vals = [int(x) for x in seq if int(x) > 0][:size]
    if len(vals) < size:
        vals.extend([int(pad_value)] * (size - len(vals)))
    return vals


def safe_float(value: object, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default
