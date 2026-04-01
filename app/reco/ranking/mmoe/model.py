from __future__ import annotations

from typing import Dict
import math

import torch
from torch import Tensor
import torch.nn as nn


MMOE_TASKS: tuple[str, ...] = ("click", "collect", "comment", "rating")


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
        gender_vocab_size: int = 0,
        age_bucket_vocab_size: int = 0,
        tag_vocab_size: int = 0,
        use_item_tag_pooling: bool = False,
        use_target_attention: bool = False,
        use_long_interest_pooling: bool = False,
    ) -> None:
        super().__init__()
        self.user_emb = nn.Embedding(max(user_vocab_size, 1), emb_dim, padding_idx=0)
        self.item_emb = nn.Embedding(max(item_vocab_size, 1), emb_dim, padding_idx=0)

        self.use_item_tag_pooling = bool(use_item_tag_pooling)
        self.use_target_attention = bool(use_target_attention)
        self.use_long_interest_pooling = bool(use_long_interest_pooling)

        self.gender_emb: nn.Embedding | None = None
        if int(gender_vocab_size) > 0:
            self.gender_emb = nn.Embedding(max(int(gender_vocab_size), 1), emb_dim, padding_idx=0)

        self.age_bucket_emb: nn.Embedding | None = None
        if int(age_bucket_vocab_size) > 0:
            self.age_bucket_emb = nn.Embedding(max(int(age_bucket_vocab_size), 1), emb_dim, padding_idx=0)

        self.tag_emb: nn.Embedding | None = None
        if int(tag_vocab_size) > 0 and (self.use_item_tag_pooling or self.use_long_interest_pooling):
            self.tag_emb = nn.Embedding(max(int(tag_vocab_size), 1), emb_dim, padding_idx=0)

        d_in = emb_dim * 2 + int(num_numeric_features)
        if self.gender_emb is not None:
            d_in += emb_dim
        if self.age_bucket_emb is not None:
            d_in += emb_dim
        if self.use_item_tag_pooling:
            d_in += emb_dim
        if self.use_target_attention:
            d_in += emb_dim
        if self.use_long_interest_pooling:
            d_in += emb_dim

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

    def _masked_mean_pool(self, emb: Tensor, ids: Tensor) -> Tensor:
        mask = (ids != 0).float().unsqueeze(-1)
        summed = torch.sum(emb * mask, dim=1)
        denom = torch.sum(mask, dim=1).clamp_min(1.0)
        return summed / denom

    def _target_attention(self, query_item_emb: Tensor, hist_item_ids: Tensor) -> Tensor:
        # query: [B, D], hist: [B, L]
        hist_emb = self.item_emb(hist_item_ids)
        mask = hist_item_ids != 0

        scores = torch.sum(hist_emb * query_item_emb.unsqueeze(1), dim=-1) / math.sqrt(float(hist_emb.shape[-1]))
        scores = scores.masked_fill(~mask, -1e9)
        weights = torch.softmax(scores, dim=1)
        weights = weights * mask.float()
        weights = weights / weights.sum(dim=1, keepdim=True).clamp_min(1e-8)
        return torch.sum(hist_emb * weights.unsqueeze(-1), dim=1)

    def forward(
        self,
        user_idx: Tensor,
        item_idx: Tensor,
        numeric_x: Tensor,
        gender_idx: Tensor | None = None,
        age_bucket_idx: Tensor | None = None,
        item_tag_ids: Tensor | None = None,
        short_hist_item_ids: Tensor | None = None,
        long_interest_tag_ids: Tensor | None = None,
    ) -> Dict[str, Tensor]:
        u = self.user_emb(user_idx)
        i = self.item_emb(item_idx)
        parts = [u, i, numeric_x]

        if self.gender_emb is not None:
            if gender_idx is None:
                gender_idx = torch.zeros_like(user_idx)
            parts.append(self.gender_emb(gender_idx))

        if self.age_bucket_emb is not None:
            if age_bucket_idx is None:
                age_bucket_idx = torch.zeros_like(user_idx)
            parts.append(self.age_bucket_emb(age_bucket_idx))

        if self.use_item_tag_pooling:
            if self.tag_emb is not None and item_tag_ids is not None:
                parts.append(self._masked_mean_pool(self.tag_emb(item_tag_ids), item_tag_ids))
            else:
                parts.append(torch.zeros_like(i))

        if self.use_target_attention:
            if short_hist_item_ids is not None:
                parts.append(self._target_attention(i, short_hist_item_ids))
            else:
                parts.append(torch.zeros_like(i))

        if self.use_long_interest_pooling:
            if self.tag_emb is not None and long_interest_tag_ids is not None:
                parts.append(self._masked_mean_pool(self.tag_emb(long_interest_tag_ids), long_interest_tag_ids))
            else:
                parts.append(torch.zeros_like(i))

        d_in = torch.cat(parts, dim=1)

        expert_stack = torch.stack([expert(d_in) for expert in self.experts], dim=1)

        out: Dict[str, Tensor] = {}
        for task in MMOE_TASKS:
            gate_w = self.gates[task](d_in).unsqueeze(-1)
            mixed = torch.sum(expert_stack * gate_w, dim=1)
            logits = self.towers[task](mixed).squeeze(1)
            out[task] = torch.sigmoid(logits)
        return out
