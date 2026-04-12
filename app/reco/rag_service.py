from __future__ import annotations

import hashlib
import json
import logging
import threading
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Iterator, Sequence

import numpy as np
from sqlalchemy import Engine, bindparam, create_engine, text

from app.common.redis_cache import get_redis_client
from app.common.runtime_health import mark_component_error, mark_component_success
from app.common.settings import Settings
from app.reco.rag_clients import OpenAICompatConfig, create_embedding, stream_chat_completion


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RagEvidence:
    faiss_id: int
    movie_id: int
    title: str
    year: int | None
    summary: str
    chunk_text: str
    score: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "faiss_id": int(self.faiss_id),
            "movie_id": int(self.movie_id),
            "title": self.title,
            "year": self.year,
            "summary": self.summary,
            "score": float(self.score),
        }


class MovieRagService:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._lock = threading.RLock()
        self._engine: Engine | None = None
        self._faiss = None
        self._index = None
        self._dim: int | None = None
        self._movie_by_faiss: dict[int, int] = {}
        self._faiss_by_movie: dict[int, int] = {}
        self._chunk_by_faiss: dict[int, str] = {}
        self._index_ready = False

    def _ensure_engine(self) -> Engine:
        if self._engine is not None:
            return self._engine
        mysql_dsn = self._settings.core.mysql_dsn
        if not mysql_dsn:
            raise RuntimeError("MYSQL_DSN is required for rag retrieval")
        self._engine = create_engine(mysql_dsn, pool_pre_ping=True)
        return self._engine

    def _ensure_faiss_module(self):
        if self._faiss is not None:
            return self._faiss
        try:
            import faiss  # type: ignore
        except Exception as exc:
            raise RuntimeError(f"faiss_import_failed: {type(exc).__name__}: {exc}") from exc
        self._faiss = faiss
        return self._faiss

    def _redis_key(self, *parts: object) -> str:
        prefix = self._settings.cache.key_prefix.strip() if self._settings.cache.key_prefix else "reco"
        return f"{prefix}:rag:{':'.join(str(x) for x in parts)}"

    def _embedding_cfg(self) -> OpenAICompatConfig:
        return OpenAICompatConfig(
            base_url=str(self._settings.rag.embedding_api_base_url or "").strip(),
            api_key=self._settings.rag.embedding_api_key,
            model=self._settings.rag.embedding_model_name,
            timeout_seconds=30.0,
        )

    def _llm_cfg(self) -> OpenAICompatConfig:
        return OpenAICompatConfig(
            base_url=str(self._settings.rag.llm_api_base_url or "").strip(),
            api_key=self._settings.rag.llm_api_key,
            model=self._settings.rag.llm_model_name,
            timeout_seconds=60.0,
        )

    @staticmethod
    def _vector_to_blob(vec: np.ndarray) -> bytes:
        return np.asarray(vec, dtype=np.float32).reshape(-1).tobytes()

    @staticmethod
    def _blob_to_vector(blob: bytes | bytearray | memoryview | None) -> np.ndarray | None:
        if blob is None:
            return None
        try:
            arr = np.frombuffer(blob, dtype=np.float32)
            if arr.size <= 0:
                return None
            return np.asarray(arr, dtype=np.float32).reshape(-1)
        except Exception:
            return None

    @staticmethod
    def _normalize(vec: np.ndarray) -> np.ndarray:
        arr = np.asarray(vec, dtype=np.float32).reshape(-1)
        norm = float(np.linalg.norm(arr))
        if norm <= 0.0:
            return arr
        return arr / norm

    def _fetch_embedding_rows(self) -> list[dict[str, Any]]:
        sql = text(
            """
            SELECT id, movie_id, chunk_text, embedding_vector
            FROM movie_embeddings
            ORDER BY id ASC
            """
        )
        with self._ensure_engine().connect() as conn:
            rows = conn.execute(sql).mappings().all()

        out: list[dict[str, Any]] = []
        for row in rows:
            emb = self._blob_to_vector(row.get("embedding_vector"))
            if emb is None:
                continue
            out.append(
                {
                    "id": int(row["id"]),
                    "movie_id": int(row["movie_id"]),
                    "chunk_text": str(row.get("chunk_text") or ""),
                    "vector": emb,
                }
            )
        return out

    def _build_index_from_rows(self, rows: Sequence[dict[str, Any]]) -> None:
        if not rows:
            with self._lock:
                self._index = None
                self._dim = None
                self._movie_by_faiss.clear()
                self._faiss_by_movie.clear()
                self._chunk_by_faiss.clear()
                self._index_ready = True
            mark_component_success("rag", details={"rows": 0})
            return

        faiss = self._ensure_faiss_module()
        vectors: list[np.ndarray] = []
        ids: list[int] = []
        movie_by_faiss: dict[int, int] = {}
        faiss_by_movie: dict[int, int] = {}
        chunk_by_faiss: dict[int, str] = {}

        dim: int | None = None
        for row in rows:
            vec = self._normalize(np.asarray(row["vector"], dtype=np.float32).reshape(-1))
            if vec.size <= 0:
                continue
            if dim is None:
                dim = int(vec.size)
            if int(vec.size) != int(dim):
                continue
            faiss_id = int(row["id"])
            movie_id = int(row["movie_id"])
            vectors.append(vec)
            ids.append(faiss_id)
            movie_by_faiss[faiss_id] = movie_id
            faiss_by_movie[movie_id] = faiss_id
            chunk_by_faiss[faiss_id] = str(row.get("chunk_text") or "")

        if not vectors or dim is None:
            with self._lock:
                self._index = None
                self._dim = None
                self._movie_by_faiss = {}
                self._faiss_by_movie = {}
                self._chunk_by_faiss = {}
                self._index_ready = True
            mark_component_success("rag", details={"rows": 0, "reason": "no_valid_vectors"})
            return

        hnsw_m = max(8, int(self._settings.rag.index_hnsw_m))
        base = faiss.IndexHNSWFlat(int(dim), hnsw_m, faiss.METRIC_INNER_PRODUCT)
        base.hnsw.efSearch = max(32, int(self._settings.rag.index_hnsw_ef_search))
        index = faiss.IndexIDMap2(base)
        matrix = np.stack(vectors).astype(np.float32)
        ids_arr = np.asarray(ids, dtype=np.int64)
        index.add_with_ids(matrix, ids_arr)

        with self._lock:
            self._index = index
            self._dim = int(dim)
            self._movie_by_faiss = movie_by_faiss
            self._faiss_by_movie = faiss_by_movie
            self._chunk_by_faiss = chunk_by_faiss
            self._index_ready = True

        mark_component_success("rag", details={"rows": int(len(vectors)), "dim": int(dim), "m": int(hnsw_m)})

    def load_from_mysql(self) -> None:
        try:
            rows = self._fetch_embedding_rows()
            self._build_index_from_rows(rows)
            logger.info("RAG index loaded from MySQL, rows=%s", len(rows))
        except Exception as exc:
            mark_component_error("rag", exc, details={"stage": "load_from_mysql"})
            logger.exception("RAG index load failed")
            raise

    def ensure_loaded(self) -> None:
        if self._index_ready:
            return
        with self._lock:
            if self._index_ready:
                return
        self.load_from_mysql()

    def _fetch_movie(self, movie_id: int) -> dict[str, Any] | None:
        sql = text(
            """
            SELECT movie_id, title, year, COALESCE(summary, '') AS summary
            FROM movie
            WHERE movie_id = :movie_id
              AND deleted_at IS NULL
            LIMIT 1
            """
        )
        with self._ensure_engine().connect() as conn:
            row = conn.execute(sql, {"movie_id": int(movie_id)}).mappings().first()
        if row is None:
            return None
        return {
            "movie_id": int(row["movie_id"]),
            "title": str(row.get("title") or "").strip(),
            "year": int(row["year"]) if row.get("year") is not None else None,
            "summary": str(row.get("summary") or "").strip(),
        }

    @staticmethod
    def _build_chunk_text(movie: dict[str, Any]) -> str:
        title = movie.get("title") or "Unknown"
        year = movie.get("year")
        summary = movie.get("summary") or ""
        return (
            f"movie_title: {title}\n"
            f"release_year: {year if year is not None else 'unknown'}\n"
            f"summary: {summary}"
        )

    def _upsert_embedding_row(self, *, movie_id: int, chunk_text: str, vector: np.ndarray) -> int:
        select_sql = text(
            """
            SELECT id
            FROM movie_embeddings
            WHERE movie_id = :movie_id
            ORDER BY id DESC
            LIMIT 1
            """
        )
        update_sql = text(
            """
            UPDATE movie_embeddings
            SET chunk_text = :chunk_text, embedding_vector = :embedding_vector
            WHERE id = :id
            """
        )
        insert_sql = text(
            """
            INSERT INTO movie_embeddings(movie_id, chunk_text, embedding_vector)
            VALUES (:movie_id, :chunk_text, :embedding_vector)
            """
        )

        blob = self._vector_to_blob(vector)
        with self._ensure_engine().begin() as conn:
            row = conn.execute(select_sql, {"movie_id": int(movie_id)}).mappings().first()
            if row is not None:
                emb_id = int(row["id"])
                conn.execute(update_sql, {"id": emb_id, "chunk_text": chunk_text, "embedding_vector": blob})
                return emb_id

            rs = conn.execute(
                insert_sql,
                {
                    "movie_id": int(movie_id),
                    "chunk_text": chunk_text,
                    "embedding_vector": blob,
                },
            )
            if rs.lastrowid is None:
                raise RuntimeError("movie_embeddings_insert_failed: empty_insert_id")
            return int(rs.lastrowid)

    def _sync_index_item(self, *, emb_id: int, movie_id: int, chunk_text: str, vector: np.ndarray) -> None:
        self.ensure_loaded()
        faiss = self._ensure_faiss_module()
        vec = self._normalize(np.asarray(vector, dtype=np.float32).reshape(-1))
        if vec.size <= 0:
            raise RuntimeError("embedding_vector_empty")

        with self._lock:
            if self._dim is None:
                dim = int(vec.size)
                hnsw_m = max(8, int(self._settings.rag.index_hnsw_m))
                base = faiss.IndexHNSWFlat(dim, hnsw_m, faiss.METRIC_INNER_PRODUCT)
                base.hnsw.efSearch = max(32, int(self._settings.rag.index_hnsw_ef_search))
                self._index = faiss.IndexIDMap2(base)
                self._dim = dim

            if int(vec.size) != int(self._dim):
                raise RuntimeError(f"embedding_dim_mismatch: expected={self._dim}, got={vec.size}")

            if self._index is None:
                raise RuntimeError("rag_index_not_initialized")

            old_id = self._faiss_by_movie.get(int(movie_id))
            if old_id is not None and int(old_id) != int(emb_id):
                try:
                    self._index.remove_ids(np.asarray([int(old_id)], dtype=np.int64))
                except Exception:
                    logger.warning("RAG remove old vector failed, old_id=%s", old_id, exc_info=True)

            self._index.remove_ids(np.asarray([int(emb_id)], dtype=np.int64))
            self._index.add_with_ids(vec.reshape(1, -1), np.asarray([int(emb_id)], dtype=np.int64))
            self._movie_by_faiss[int(emb_id)] = int(movie_id)
            self._faiss_by_movie[int(movie_id)] = int(emb_id)
            self._chunk_by_faiss[int(emb_id)] = chunk_text

    def upsert_one(self, movie_id: int) -> int:
        movie = self._fetch_movie(int(movie_id))
        if movie is None:
            raise RuntimeError(f"movie_not_found: {movie_id}")

        chunk_text = self._build_chunk_text(movie)
        emb = create_embedding(cfg=self._embedding_cfg(), text=chunk_text)
        vector = np.asarray(emb, dtype=np.float32).reshape(-1)
        if vector.size <= 0:
            raise RuntimeError("embedding_vector_empty")

        emb_id = self._upsert_embedding_row(movie_id=int(movie_id), chunk_text=chunk_text, vector=vector)
        self._sync_index_item(emb_id=emb_id, movie_id=int(movie_id), chunk_text=chunk_text, vector=vector)
        self._cache_faiss_mapping(emb_id=int(emb_id), movie_id=int(movie_id))
        return int(emb_id)

    def _query_cache_key(self, query: str, k: int) -> str:
        digest = hashlib.sha1(f"{query}|{k}".encode("utf-8")).hexdigest()
        return self._redis_key("query", digest)

    def _cache_faiss_mapping(self, *, emb_id: int, movie_id: int) -> None:
        client = get_redis_client(self._settings)
        if client is None:
            return
        ttl = max(60, int(self._settings.rag.redis_result_ttl_seconds))
        client.set(self._redis_key("map", int(emb_id)), str(int(movie_id)), ex=ttl)

    def _resolve_movie_id(self, faiss_id: int) -> int | None:
        client = get_redis_client(self._settings)
        if client is not None:
            raw = client.get(self._redis_key("map", int(faiss_id)))
            if raw:
                try:
                    return int(raw)
                except Exception:
                    pass

        movie_id = self._movie_by_faiss.get(int(faiss_id))
        if movie_id is not None:
            self._cache_faiss_mapping(emb_id=int(faiss_id), movie_id=int(movie_id))
        return movie_id

    def _search_faiss_ids(self, *, query: str, k: int) -> list[tuple[int, float]]:
        self.ensure_loaded()
        if self._index is None or self._dim is None:
            return []

        client = get_redis_client(self._settings)
        if client is not None:
            cached = client.get(self._query_cache_key(query, k))
            if cached:
                try:
                    arr = json.loads(cached)
                    if isinstance(arr, list):
                        out: list[tuple[int, float]] = []
                        for item in arr:
                            if isinstance(item, list) and len(item) == 2:
                                out.append((int(item[0]), float(item[1])))
                        if out:
                            return out
                except Exception:
                    logger.warning("RAG query cache parse failed", exc_info=True)

        vec = np.asarray(create_embedding(cfg=self._embedding_cfg(), text=query), dtype=np.float32).reshape(-1)
        vec = self._normalize(vec)

        with self._lock:
            if self._index is None or self._dim is None:
                return []
            if int(vec.size) != int(self._dim):
                raise RuntimeError(f"query_embedding_dim_mismatch: expected={self._dim}, got={vec.size}")
            scores, ids = self._index.search(vec.reshape(1, -1), int(k))

        out: list[tuple[int, float]] = []
        for faiss_id, score in zip(ids[0], scores[0]):
            fid = int(faiss_id)
            if fid < 0:
                continue
            out.append((fid, float(score)))

        if client is not None and out:
            ttl = max(60, int(self._settings.rag.redis_result_ttl_seconds))
            client.set(self._query_cache_key(query, k), json.dumps(out, ensure_ascii=False), ex=ttl)
        return out

    def _fetch_movies_by_ids(self, movie_ids: Iterable[int]) -> dict[int, dict[str, Any]]:
        ids = [int(x) for x in movie_ids if int(x) > 0]
        if not ids:
            return {}
        sql = text(
            """
            SELECT movie_id, title, year, COALESCE(summary, '') AS summary
            FROM movie
            WHERE movie_id IN :movie_ids
              AND deleted_at IS NULL
            """
        ).bindparams(bindparam("movie_ids", expanding=True))
        with self._ensure_engine().connect() as conn:
            rows = conn.execute(sql, {"movie_ids": ids}).mappings().all()
        out: dict[int, dict[str, Any]] = {}
        for row in rows:
            mid = int(row["movie_id"])
            out[mid] = {
                "movie_id": mid,
                "title": str(row.get("title") or ""),
                "year": int(row["year"]) if row.get("year") is not None else None,
                "summary": str(row.get("summary") or ""),
            }
        return out

    def retrieve_evidence(self, *, query: str, n: int) -> list[RagEvidence]:
        ann_topk = max(int(n), int(self._settings.rag.ann_topk_default))
        pairs = self._search_faiss_ids(query=query, k=ann_topk)
        if not pairs:
            return []

        movie_ids: list[int] = []
        resolved: list[tuple[int, int, float]] = []
        for faiss_id, score in pairs:
            movie_id = self._resolve_movie_id(faiss_id)
            if movie_id is None:
                continue
            movie_ids.append(int(movie_id))
            resolved.append((int(faiss_id), int(movie_id), float(score)))

        movie_map = self._fetch_movies_by_ids(movie_ids)
        out: list[RagEvidence] = []
        seen: set[int] = set()
        for faiss_id, movie_id, score in resolved:
            if movie_id in seen:
                continue
            meta = movie_map.get(movie_id)
            if meta is None:
                continue
            seen.add(movie_id)
            out.append(
                RagEvidence(
                    faiss_id=faiss_id,
                    movie_id=movie_id,
                    title=str(meta.get("title") or ""),
                    year=meta.get("year"),
                    summary=str(meta.get("summary") or ""),
                    chunk_text=self._chunk_by_faiss.get(faiss_id, ""),
                    score=score,
                )
            )
            if len(out) >= int(n):
                break
        return out

    def stream_answer(self, *, query: str, n: int) -> tuple[list[int], Iterator[str]]:
        evidence = self.retrieve_evidence(query=query, n=n)
        cited_ids = [int(e.movie_id) for e in evidence]

        context_rows: list[str] = []
        for idx, item in enumerate(evidence, start=1):
            context_rows.append(
                f"[{idx}] movie_id={item.movie_id}, title={item.title}, year={item.year}, score={item.score:.4f}\n"
                f"summary: {item.summary}\n"
                f"retrieved_chunk: {item.chunk_text}"
            )
        context = "\n\n".join(context_rows) if context_rows else "No supporting movie evidence was retrieved."

        system_prompt = (
            "You are a movie recommendation assistant. "
            "Answer in Chinese. "
            "Ground your answer on retrieval evidence and state uncertainty when evidence is weak."
        )
        user_prompt = f"user_query: {query}\n\nretrieval_evidence:\n{context}"
        stream = stream_chat_completion(cfg=self._llm_cfg(), system_prompt=system_prompt, user_prompt=user_prompt)
        return cited_ids, stream


_service: MovieRagService | None = None
_service_lock = threading.RLock()


def get_movie_rag_service(settings: Settings) -> MovieRagService:
    global _service
    with _service_lock:
        if _service is not None:
            return _service
        _service = MovieRagService(settings)
        return _service
