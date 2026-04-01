from __future__ import annotations

import torch
import torch.nn.functional as F
from torch import nn


class FeatureTwoTowerEncoder(nn.Module):
    def __init__(
        self,
        *,
        user_count: int,
        item_count: int,
        tag_count: int,
        dim: int,
        stats_dim: int,
        seed: int,
    ) -> None:
        super().__init__()
        self.user_id_table = nn.Embedding(user_count, dim)
        self.item_id_table = nn.Embedding(item_count, dim)
        self.gender_table = nn.Embedding(3, dim)
        self.age_bucket_table = nn.Embedding(7, dim)
        self.register_bucket_table = nn.Embedding(6, dim)
        self.tag_table = nn.Embedding(tag_count, dim)
        self.item_stats_proj = nn.Linear(stats_dim, dim)
        self.user_proj = nn.Linear(dim * 5, dim)
        self.item_proj = nn.Linear(dim * 3, dim)

        gen = torch.Generator(device="cpu")
        gen.manual_seed(int(seed))
        with torch.no_grad():
            self.user_id_table.weight.normal_(mean=0.0, std=0.05, generator=gen)
            self.item_id_table.weight.normal_(mean=0.0, std=0.05, generator=gen)
            self.gender_table.weight.normal_(mean=0.0, std=0.05, generator=gen)
            self.age_bucket_table.weight.normal_(mean=0.0, std=0.05, generator=gen)
            self.register_bucket_table.weight.normal_(mean=0.0, std=0.05, generator=gen)
            self.tag_table.weight.normal_(mean=0.0, std=0.05, generator=gen)

    @staticmethod
    def masked_mean(x: torch.Tensor, mask: torch.Tensor) -> torch.Tensor:
        m = mask.unsqueeze(-1).to(dtype=x.dtype)
        denom = torch.clamp(m.sum(dim=1), min=1e-6)
        return (x * m).sum(dim=1) / denom

    def encode_user_inputs(
        self,
        *,
        user_id_idx: torch.Tensor,
        gender_idx: torch.Tensor,
        age_bucket_idx: torch.Tensor,
        register_bucket_idx: torch.Tensor,
        seq_item_idx: torch.Tensor,
        seq_mask: torch.Tensor,
    ) -> torch.Tensor:
        uid_vec = self.user_id_table(user_id_idx)
        gender_vec = self.gender_table(gender_idx)
        age_vec = self.age_bucket_table(age_bucket_idx)
        reg_vec = self.register_bucket_table(register_bucket_idx)

        seq_emb = self.item_id_table(seq_item_idx)
        seq_vec = self.masked_mean(seq_emb, seq_mask)

        user_input = torch.cat([uid_vec, gender_vec, age_vec, reg_vec, seq_vec], dim=1)
        return F.normalize(self.user_proj(user_input), p=2, dim=1, eps=1e-12)

    def encode_item_inputs(
        self,
        *,
        item_id_idx: torch.Tensor,
        tag_idx: torch.Tensor,
        tag_mask: torch.Tensor,
        stats: torch.Tensor,
    ) -> torch.Tensor:
        iid_vec = self.item_id_table(item_id_idx)
        tag_emb = self.tag_table(tag_idx)
        tag_vec = self.masked_mean(tag_emb, tag_mask)
        stats_vec = self.item_stats_proj(stats)
        item_input = torch.cat([iid_vec, tag_vec, stats_vec], dim=1)
        return F.normalize(self.item_proj(item_input), p=2, dim=1, eps=1e-12)

    def forward(
        self,
        *,
        user_id_idx: torch.Tensor,
        user_gender_idx: torch.Tensor,
        user_age_idx: torch.Tensor,
        user_register_idx: torch.Tensor,
        user_seq_item_idx: torch.Tensor,
        user_seq_mask: torch.Tensor,
        pos_item_id_idx: torch.Tensor,
        pos_tag_idx: torch.Tensor,
        pos_tag_mask: torch.Tensor,
        pos_stats: torch.Tensor,
        neg_item_id_idx: torch.Tensor,
        neg_tag_idx: torch.Tensor,
        neg_tag_mask: torch.Tensor,
        neg_stats: torch.Tensor,
    ) -> tuple[torch.Tensor, torch.Tensor]:
        pu = self.encode_user_inputs(
            user_id_idx=user_id_idx,
            gender_idx=user_gender_idx,
            age_bucket_idx=user_age_idx,
            register_bucket_idx=user_register_idx,
            seq_item_idx=user_seq_item_idx,
            seq_mask=user_seq_mask,
        )
        pi = self.encode_item_inputs(
            item_id_idx=pos_item_id_idx,
            tag_idx=pos_tag_idx,
            tag_mask=pos_tag_mask,
            stats=pos_stats,
        )
        pj = self.encode_item_inputs(
            item_id_idx=neg_item_id_idx,
            tag_idx=neg_tag_idx,
            tag_mask=neg_tag_mask,
            stats=neg_stats,
        )
        logits = (pu * pi).sum(dim=1) - (pu * pj).sum(dim=1)
        l2 = (pu.pow(2).sum(dim=1) + pi.pow(2).sum(dim=1) + pj.pow(2).sum(dim=1)).mean()
        return logits, l2
