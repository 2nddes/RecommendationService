from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Any, Dict, List, Sequence

import torch
from torch import Tensor
import torch.nn as nn

from app.common.settings import Settings
from app.reco.ranking.base import Ranker
from app.reco.ranking.xgb_features import fetch_movie_features
from app.reco.types import Candidate, RankedItem, RequestContext


MMOE_TASKS: tuple[str, ...] = ("click", "collect", "comment", "rating")


def load_latest_local_model(settings: Settings) -> str | None:
    configured = str(settings.mmoe_model_path or "").strip()
    active_path = configured or os.path.join("data", "models", "mmoe_latest.pt")

    if os.path.exists(active_path):
        return active_path

    artifact_dir = os.path.join("data", "artifacts", "mmoe")
    if not os.path.isdir(artifact_dir):
        return None

    candidates = [
        os.path.join(artifact_dir, name)
        for name in os.listdir(artifact_dir)
        if name.endswith(".pt")
    ]
    if not candidates:
        return None

    latest = max(candidates, key=lambda p: os.path.getmtime(p))
    os.makedirs(os.path.dirname(active_path) or ".", exist_ok=True)
    tmp = active_path + ".tmp"
    with open(latest, "rb") as src, open(tmp, "wb") as dst:
        dst.write(src.read())
    os.replace(tmp, active_path)
    return active_path


class MMoENet(nn.Module):
    def __init__(
        self,
        *,
        user_vocab_size: int,
        item_vocab_size: int,
        num_numeric_features: int,
        emb_dim: int,
        num_experts: int,
        expert_hidden_dim: int,
        tower_hidden_dim: int,
    ) -> None:
        super().__init__()
        self.user_emb = nn.Embedding(max(user_vocab_size, 1), emb_dim)
        self.item_emb = nn.Embedding(max(item_vocab_size, 1), emb_dim)

        d_in = emb_dim * 2 + int(num_numeric_features)

        self.experts = nn.ModuleList(
            [
                nn.Sequential(
                    nn.Linear(d_in, expert_hidden_dim),
                    nn.ReLU(),
                    nn.Linear(expert_hidden_dim, expert_hidden_dim),
                    nn.ReLU(),
                )
                for _ in range(max(num_experts, 1))
            ]
        )

        self.gates = nn.ModuleDict(
            {
                task: nn.Sequential(
                    nn.Linear(d_in, max(num_experts, 1)),
                    nn.Softmax(dim=1),
                )
                for task in MMOE_TASKS
            }
        )

        self.towers = nn.ModuleDict(
            {
                task: nn.Sequential(
                    nn.Linear(expert_hidden_dim, tower_hidden_dim),
                    nn.ReLU(),
                    nn.Linear(tower_hidden_dim, 1),
                )
                for task in MMOE_TASKS
            }
        )

    def forward(self, user_idx: Tensor, item_idx: Tensor, numeric_x: Tensor) -> Dict[str, Tensor]:
        u = self.user_emb(user_idx)
        i = self.item_emb(item_idx)
        d_in = torch.cat([u, i, numeric_x], dim=1)

        expert_stack = torch.stack([expert(d_in) for expert in self.experts], dim=1)

        out: Dict[str, Tensor] = {}
        for task in MMOE_TASKS:
            gate_w = self.gates[task](d_in).unsqueeze(-1)
            mixed = torch.sum(expert_stack * gate_w, dim=1)
            logits = self.towers[task](mixed).squeeze(1)
            out[task] = torch.sigmoid(logits)
        return out


@dataclass(frozen=True)
class MMoERanker(Ranker):
    model_path: str | None = None
    use_mysql_features: bool = True
    mysql_dsn: str | None = None

    @property
    def name(self) -> str:
        return "mmoe"

    def rank(self, ctx: RequestContext, candidates: List[Candidate]) -> List[RankedItem]:
        if not candidates:
            return []

        if not self.model_path:
            raise RuntimeError("mmoe_model_path_is_empty")

        bundle = torch.load(self.model_path, map_location="cpu")
        model = self._build_model_from_bundle(bundle)
        model.eval()

        movie_features_by_id = (
            fetch_movie_features([c.item_id for c in candidates], mysql_dsn=self.mysql_dsn)
            if self.use_mysql_features
            else {}
        )

        user_ids, item_ids, numeric_rows = self._build_infer_tensors(
            ctx=ctx,
            candidates=candidates,
            movie_features_by_id=movie_features_by_id,
            user_index=bundle["user_index"],
            item_index=bundle["item_index"],
            feature_order=[str(x) for x in bundle.get("feature_order") or bundle_feature_order()],
            feature_stats=bundle["feature_stats"],
        )

        with torch.no_grad():
            pred = model(user_ids, item_ids, numeric_rows)

        p_click = pred["click"]
        p_collect = pred["collect"]
        p_comment = pred["comment"]
        p_rating = pred["rating"]
        score = (p_click + p_collect + p_comment + p_rating) / 4.0

        ranked = [
            RankedItem(item_id=int(c.item_id), score=float(s), reason="mmoe")
            for c, s in zip(candidates, score.tolist())
        ]
        ranked.sort(key=lambda x: x.score, reverse=True)
        return ranked

    def _build_model_from_bundle(self, bundle: Dict[str, Any]) -> MMoENet:
        model_meta = bundle["model_meta"]
        model = MMoENet(
            user_vocab_size=int(model_meta["user_vocab_size"]),
            item_vocab_size=int(model_meta["item_vocab_size"]),
            num_numeric_features=int(model_meta["num_numeric_features"]),
            emb_dim=int(model_meta["emb_dim"]),
            num_experts=int(model_meta["num_experts"]),
            expert_hidden_dim=int(model_meta["expert_hidden_dim"]),
            tower_hidden_dim=int(model_meta["tower_hidden_dim"]),
        )
        model.load_state_dict(bundle["state_dict"])
        return model

    def _build_infer_tensors(
        self,
        *,
        ctx: RequestContext,
        candidates: Sequence[Candidate],
        movie_features_by_id: Dict[int, Dict[str, Any]],
        user_index: Dict[int, int],
        item_index: Dict[int, int],
        feature_order: Sequence[str],
        feature_stats: Dict[str, Dict[str, float]],
    ) -> tuple[Tensor, Tensor, Tensor]:
        rows: List[List[float]] = []
        user_idx: List[int] = []
        item_idx: List[int] = []

        src_names = [
            "src_user_collection",
            "src_user_high_rating_similar",
            "src_user_interest_tag",
            "src_item_similar_by_tags",
            "src_two_tower",
        ]

        uid = int(ctx.user_id) if ctx.user_id is not None else 0
        uid_idx = int(user_index.get(uid, 0))

        for c in candidates:
            mid = int(c.item_id)
            item_idx.append(int(item_index.get(mid, 0)))
            user_idx.append(uid_idx)

            movie_f = movie_features_by_id.get(mid) or {}
            rating_cnt = float(movie_f.get("rating_count") or 0.0)
            rating_avg = float(movie_f.get("rating_avg") or 0.0)
            year = float(movie_f.get("year") or 0.0)
            duration = float(movie_f.get("duration_min") or 0.0)

            one_hot = [1.0 if str(c.source) == s.replace("src_", "") else 0.0 for s in src_names]
            raw = {
                "recall_score": float(c.score),
                "movie_rating_avg": rating_avg,
                "movie_rating_count": rating_cnt,
                "movie_year": year,
                "movie_duration_min": duration,
                **{k: v for k, v in zip(src_names, one_hot)},
            }

            row = []
            for name in feature_order:
                stats = feature_stats.get(name, {"mean": 0.0, "std": 1.0})
                mean = float(stats.get("mean", 0.0))
                std = float(stats.get("std", 1.0))
                std = std if abs(std) > 1e-8 else 1.0
                row.append((float(raw.get(name, 0.0)) - mean) / std)
            rows.append(row)

        return (
            torch.tensor(user_idx, dtype=torch.long),
            torch.tensor(item_idx, dtype=torch.long),
            torch.tensor(rows, dtype=torch.float32),
        )


def bundle_feature_order() -> List[str]:
    return [
        "recall_score",
        "movie_rating_avg",
        "movie_rating_count",
        "movie_year",
        "movie_duration_min",
        "src_user_collection",
        "src_user_high_rating_similar",
        "src_user_interest_tag",
        "src_item_similar_by_tags",
        "src_two_tower",
    ]
