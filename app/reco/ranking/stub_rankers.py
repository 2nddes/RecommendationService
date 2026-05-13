from __future__ import annotations

from dataclasses import dataclass
from typing import Any, List
import time

import numpy as np

from app.common.mysql_engine import get_shared_mysql_engine
from app.reco.ranking.base import Ranker
from app.reco.types import Candidate, RankedItem, RequestContext


_cf_cache: dict[str, dict[str, Any]] = {}


def _normalize(values: np.ndarray) -> np.ndarray:
    if values.size == 0:
        return values
    v_min = float(np.min(values))
    v_max = float(np.max(values))
    if v_max - v_min <= 1e-12:
        return np.zeros_like(values)
    return (values - v_min) / (v_max - v_min)


@dataclass(frozen=True)
class CollaborativeFilteringRanker(Ranker):
    """Item-CF 排序器（pandas + scikit-learn）。

    - 输入：召回候选集
    - 输出：基于用户历史交互与候选物品相似度的重排结果
    - 冷启动：用户无历史/模型构建失败时回退为召回分 + 热度
    """

    mysql_dsn: str | None = None
    training_window_days: int = 365
    max_interactions: int = 250000
    cache_ttl_s: int = 120

    @property
    def name(self) -> str:
        return "cf"

    def rank(self, ctx: RequestContext, candidates: List[Candidate]) -> List[RankedItem]:
        if not candidates:
            return []

        model = self._get_or_build_model()
        if model is None:
            # return self._fallback_rank(candidates, reason="cf_fallback_no_model")
            return []

        if ctx.user_id is None:
            # return self._fallback_rank(candidates, reason="cf_fallback_no_user")
            return []

        scored = self._score_candidates_item_cf(ctx, candidates, model)
        if scored is None:
            # return self._fallback_rank(candidates, reason="cf_fallback_cold_user")
            return []

        return scored

    def _get_or_build_model(self) -> dict[str, Any] | None:
        dsn = (self.mysql_dsn or "").strip()
        if not dsn:
            return None

        now = time.monotonic()
        cached = _cf_cache.get(dsn)
        if cached is not None and now - float(cached.get("built_at", 0.0)) < float(self.cache_ttl_s):
            return cached

        model = self._build_model(dsn)
        if model is None:
            return None
        model["built_at"] = now
        _cf_cache[dsn] = model
        return model

    def _build_model(self, dsn: str) -> dict[str, Any] | None:
        """从 MySQL 拉取交互数据并构建 Item-CF 所需矩阵。"""

        import pandas as pd
        from scipy.sparse import csr_matrix

        engine = get_shared_mysql_engine(dsn)
        if engine is None:
            return None

        action_sql = """
        SELECT uc.user_id, uc.movie_id, uc.created_at
        FROM user_click uc
        WHERE uc.movie_id IS NOT NULL
        ORDER BY uc.created_at DESC
        LIMIT %(limit)s
        """
        comment_sql = """
        SELECT mc.user_id, mc.movie_id, mc.created_at
        FROM movie_comment mc
        WHERE mc.movie_id IS NOT NULL
            AND mc.deleted_at IS NULL
        ORDER BY mc.created_at DESC
        LIMIT %(limit)s
        """
        rating_sql = """
        SELECT r.user_id, r.movie_id, r.rating, r.updated_at
        FROM rating r
        WHERE r.movie_id IS NOT NULL
        ORDER BY r.updated_at DESC
        LIMIT %(limit)s
        """
        collect_sql = """
        SELECT ucm.user_id, ucm.movie_id, ucm.created_at
        FROM user_collect_movie ucm
        WHERE ucm.movie_id IS NOT NULL
        ORDER BY ucm.created_at DESC
        LIMIT %(limit)s
        """

        params = {"limit": int(self.max_interactions)}
        df_action = pd.read_sql(action_sql, engine, params=params)
        df_comment = pd.read_sql(comment_sql, engine, params=params)
        df_rating = pd.read_sql(rating_sql, engine, params=params)
        df_collect = pd.read_sql(collect_sql, engine, params=params)

        if df_action.empty and df_comment.empty and df_rating.empty and df_collect.empty:
            return None

        parts: list[pd.DataFrame] = []

        if not df_action.empty:
            df_action = df_action[["user_id", "movie_id"]].copy()
            df_action["weight"] = 0.2
            parts.append(df_action[["user_id", "movie_id", "weight"]])

        if not df_comment.empty:
            df_comment = df_comment[["user_id", "movie_id"]].copy()
            df_comment["weight"] = 0.6
            parts.append(df_comment[["user_id", "movie_id", "weight"]])

        if not df_rating.empty:
            df_rating = df_rating[["user_id", "movie_id", "rating"]].copy()
            df_rating["weight"] = (df_rating["rating"].astype(float) - 5.0) / 5.0
            parts.append(df_rating[["user_id", "movie_id", "weight"]])

        if not df_collect.empty:
            df_collect = df_collect[["user_id", "movie_id"]].copy()
            df_collect["weight"] = 1.0
            parts.append(df_collect[["user_id", "movie_id", "weight"]])

        if not parts:
            return None

        df = pd.concat(parts, axis=0, ignore_index=True)
        df["user_id"] = df["user_id"].astype(int)
        df["movie_id"] = df["movie_id"].astype(int)
        df["weight"] = df["weight"].astype(float)

        # 聚合同一(user,item)多次行为，削峰避免超大值。
        grouped = (
            df.groupby(["user_id", "movie_id"], as_index=False)["weight"]
            .sum()
            .assign(weight=lambda x: x["weight"].clip(-1.0, 2.0))
        )

        if grouped.empty:
            return None

        user_ids = grouped["user_id"].unique().tolist()
        movie_ids = grouped["movie_id"].unique().tolist()
        user_index = {uid: i for i, uid in enumerate(user_ids)}
        item_index = {mid: i for i, mid in enumerate(movie_ids)}

        row_idx = grouped["movie_id"].map(item_index).to_numpy()
        col_idx = grouped["user_id"].map(user_index).to_numpy()
        data = grouped["weight"].to_numpy(dtype=float)

        item_user = csr_matrix((data, (row_idx, col_idx)), shape=(len(item_index), len(user_index)))

        popularity = grouped.groupby("movie_id")["weight"].sum().to_dict()
        user_hist_df = grouped[grouped["weight"] > 0].copy()

        return {
            "item_user": item_user,
            "item_index": item_index,
            "user_history": user_hist_df,
            "popularity": popularity,
        }

    def _score_candidates_item_cf(
        self,
        ctx: RequestContext,
        candidates: List[Candidate],
        model: dict[str, Any],
    ) -> List[RankedItem] | None:
        """用 Item-CF 给候选打分。"""

        from sklearn.metrics.pairwise import cosine_similarity

        user_history = model["user_history"]
        item_index: dict[int, int] = model["item_index"]
        item_user = model["item_user"]
        popularity: dict[int, float] = model["popularity"]

        user_id = int(ctx.user_id) if ctx.user_id is not None else -1
        hist_df = user_history[user_history["user_id"] == user_id]
        if hist_df.empty:
            return None

        hist_item_ids = [int(x) for x in hist_df["movie_id"].tolist() if int(x) in item_index]
        if not hist_item_ids:
            return None

        hist_weights_map = {
            int(r.movie_id): float(r.weight)
            for r in hist_df[["movie_id", "weight"]].itertuples(index=False)
            if int(r.movie_id) in item_index
        }

        # 过滤无向量候选
        valid_candidates = [c for c in candidates if int(c.item_id) in item_index]
        if not valid_candidates:
            # return self._fallback_rank(candidates, reason="cf_fallback_no_item_vector")
            return []

        cand_idx = [item_index[int(c.item_id)] for c in valid_candidates]
        hist_idx = [item_index[mid] for mid in hist_item_ids]

        sim = cosine_similarity(item_user[cand_idx], item_user[hist_idx])

        cf_scores: list[float] = []
        for row_i, c in enumerate(valid_candidates):
            sims = sim[row_i]
            # 取 top-30 近邻，按用户历史强度加权
            if sims.size == 0:
                cf_scores.append(0.0)
                continue

            order = np.argsort(sims)[::-1][:30]
            num = 0.0
            den = 0.0
            for idx in order:
                mid = hist_item_ids[int(idx)]
                w = max(0.0, hist_weights_map[mid])
                s = max(0.0, sims[idx])
                num += s * w
                den += w
            cf_scores.append(num / den if den > 0 else 0.0)

        base_scores = np.asarray([c.score for c in valid_candidates], dtype=float)
        pop_scores = np.asarray([popularity[c.item_id] for c in valid_candidates], dtype=float)
        cf_arr = np.asarray(cf_scores, dtype=float)

        cf_norm = _normalize(cf_arr)
        base_norm = _normalize(base_scores)
        pop_norm = _normalize(pop_scores)

        final = 0.72 * cf_norm + 0.18 * base_norm + 0.10 * pop_norm

        ranked = [
            RankedItem(item_id=int(c.item_id), score=float(s), reason="cf_item_knn")
            for c, s in zip(valid_candidates, final.tolist())
        ]
        ranked.sort(key=lambda x: x.score, reverse=True)

        # 追加未建模候选，避免丢召回
        ranked_ids = {x.item_id for x in ranked}
        tail = [c for c in candidates if int(c.item_id) not in ranked_ids]
        if tail:
            # tail_ranked = self._fallback_rank(tail, reason="cf_fallback_tail")
            # ranked.extend(tail_ranked)
            pass
        return ranked

    def _fallback_rank(self, candidates: List[Candidate], *, reason: str) -> List[RankedItem]:
        """无模型/无用户行为时的可用性兜底排序。"""

        base = np.asarray([c.score for c in candidates], dtype=float)
        final = _normalize(base)
        ranked = [RankedItem(item_id=int(c.item_id), score=float(s), reason=reason) for c, s in zip(candidates, final)]
        ranked.sort(key=lambda x: x.score, reverse=True)
        return ranked


def warmup_collaborative_filtering_model(mysql_dsn: str | None) -> bool:
    """启动预热：提前构建 CF 模型缓存，避免首个请求抖动。"""

    ranker = CollaborativeFilteringRanker(mysql_dsn=mysql_dsn)
    model = ranker._get_or_build_model()
    return model is not None
