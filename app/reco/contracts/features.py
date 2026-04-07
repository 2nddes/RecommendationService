from __future__ import annotations

FEATURE_SCHEMA_VERSION = "2026-04-04.v1"

FEATURE_DICTIONARY: dict[str, list[str]] = {
    "ranking.xgb": [
        "recall_onehot_source",
        "movie_rating_avg",
        "movie_rating_count",
        "movie_year",
        "movie_duration_min",
    ],
    "ranking.mmoe": [
        "user_profile",
        "short_hist_movie_ids",
        "long_interest_tag_ids",
        "movie_static_tag_ids",
        "movie_numeric_stats",
        "recall_source",
        "recall_score",
    ],
    "recall.two_tower": [
        "user_id",
        "movie_id",
        "implicit_interaction_strength",
        "user_recent_sequence",
    ],
}


MODEL_META_VERSION = "2026-04-04.v1"


def model_feature_names(model_key: str) -> list[str]:
    return list(FEATURE_DICTIONARY.get(model_key, []))
