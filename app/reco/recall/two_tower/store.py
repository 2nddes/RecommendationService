from __future__ import annotations

from datetime import datetime
import os
import sqlite3
from typing import Mapping

import numpy as np


class ItemVectorStore:
    def __init__(self, db_path: str, dim: int) -> None:
        self.db_path = db_path
        self.dim = int(dim)
        os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        self._init_schema()

    def _init_schema(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS item_vector (
                  item_id INTEGER PRIMARY KEY,
                  dim INTEGER NOT NULL,
                  vector BLOB NOT NULL,
                  updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS meta (
                  k TEXT PRIMARY KEY,
                  v TEXT NOT NULL
                )
                """
            )
            conn.commit()

    def replace_all(self, vectors: Mapping[int, np.ndarray]) -> int:
        now = datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
        rows = [
            (int(item_id), int(self.dim), np.asarray(vec, dtype=np.float32).tobytes(), now)
            for item_id, vec in vectors.items()
        ]

        with sqlite3.connect(self.db_path) as conn:
            conn.execute("BEGIN")
            conn.execute("DELETE FROM item_vector")
            if rows:
                conn.executemany(
                    """
                    INSERT INTO item_vector(item_id, dim, vector, updated_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    rows,
                )
            conn.execute(
                """
                INSERT INTO meta(k, v) VALUES ('last_full_build_at', ?)
                ON CONFLICT(k) DO UPDATE SET v=excluded.v
                """,
                (now,),
            )
            conn.commit()
        return len(rows)

    def load_all(self) -> tuple[np.ndarray, np.ndarray]:
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT item_id, dim, vector FROM item_vector ORDER BY item_id ASC"
            ).fetchall()

        if not rows:
            return np.zeros((0,), dtype=np.int64), np.zeros((0, self.dim), dtype=np.float32)

        ids: list[int] = []
        vecs: list[np.ndarray] = []
        for item_id, dim, blob in rows:
            if int(dim) != self.dim:
                continue
            arr = np.frombuffer(blob, dtype=np.float32)
            if arr.size != self.dim:
                continue
            ids.append(int(item_id))
            vecs.append(arr.copy())

        if not ids:
            return np.zeros((0,), dtype=np.int64), np.zeros((0, self.dim), dtype=np.float32)

        return np.asarray(ids, dtype=np.int64), np.stack(vecs, axis=0).astype(np.float32, copy=False)
