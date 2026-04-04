from __future__ import annotations

import threading
import importlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List

from sqlalchemy import Engine, create_engine, text

from app.common.settings import Settings


@dataclass(frozen=True)
class RagMovieItem:
    movie_id: int
    title: str
    year: int | None
    summary: str
    score: float | None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "movie_id": int(self.movie_id),
            "title": self.title,
            "year": self.year,
            "summary": self.summary,
            "score": self.score,
        }


class MovieRagService:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._lock = threading.RLock()
        self._vector_store = None
        self._engine: Engine | None = None

    def _ensure_engine(self) -> Engine:
        if self._engine is not None:
            return self._engine

        mysql_dsn = self._settings.core.mysql_dsn
        if not mysql_dsn:
            raise RuntimeError("MYSQL_DSN is required for rag retrieval")

        self._engine = create_engine(mysql_dsn, pool_pre_ping=True)
        return self._engine

    def _repo_root(self) -> Path:
        return Path(__file__).resolve().parents[2]

    def _faiss_dir(self) -> Path:
        path = Path(self._settings.rag.faiss_dir)
        if not path.is_absolute():
            path = self._repo_root() / path
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _load_langchain_components(self):
        try:
            faiss_module = importlib.import_module("langchain_community.vectorstores")
            doc_module = importlib.import_module("langchain_core.documents")
            emb_module = importlib.import_module("langchain_huggingface")
            FAISS = getattr(faiss_module, "FAISS")
            Document = getattr(doc_module, "Document")
            HuggingFaceEmbeddings = getattr(emb_module, "HuggingFaceEmbeddings")
        except Exception as exc:
            raise RuntimeError(
                "RAG dependencies are missing. Install: langchain, langchain-community, "
                "langchain-huggingface, faiss-cpu, sentence-transformers"
            ) from exc

        return FAISS, Document, HuggingFaceEmbeddings

    def _embeddings(self):
        _, _, HuggingFaceEmbeddings = self._load_langchain_components()
        return HuggingFaceEmbeddings(
            model_name=self._settings.rag.embedding_model_name,
            model_kwargs={"device": "cpu"},
            encode_kwargs={"normalize_embeddings": True},
        )

    def _fetch_movie_rows(self) -> List[Dict[str, Any]]:
        engine = self._ensure_engine()
        sql = text(
            """
            SELECT
              m.movie_id,
              m.title,
              m.year,
              COALESCE(m.summary, '') AS summary,
              (COALESCE(m.rating_sum, 0) / NULLIF(m.rating_count, 0)) AS rating_avg,
              COALESCE(m.rating_count, 0) AS rating_count
            FROM movie m
            WHERE m.status = 'published'
              AND m.deleted_at IS NULL
            ORDER BY m.movie_id DESC
            LIMIT :limit
            """
        )

        with engine.connect() as conn:
            rows = conn.execute(sql, {"limit": self._settings.rag.build_limit})
            out: List[Dict[str, Any]] = []
            for row in rows:
                m = row._mapping
                out.append(
                    {
                        "movie_id": int(m["movie_id"]),
                        "title": str(m["title"] or "").strip(),
                        "year": int(m["year"]) if m["year"] is not None else None,
                        "summary": str(m["summary"] or "").strip(),
                        "rating_avg": float(m["rating_avg"]) if m["rating_avg"] is not None else None,
                        "rating_count": int(m["rating_count"] or 0),
                    }
                )
            return out

    def _build_documents(self, rows: Iterable[Dict[str, Any]]):
        _, Document, _ = self._load_langchain_components()
        docs = []
        for row in rows:
            title = row["title"] or "未知片名"
            summary = row["summary"] or ""
            year = row["year"]
            rating_avg = row["rating_avg"]
            rating_count = row["rating_count"]

            content = (
                f"电影标题: {title}\n"
                f"上映年份: {year if year is not None else '未知'}\n"
                f"评分均值: {rating_avg if rating_avg is not None else '暂无'}\n"
                f"评分人数: {rating_count}\n"
                f"剧情简介: {summary}"
            )

            docs.append(
                Document(
                    page_content=content,
                    metadata={
                        "movie_id": row["movie_id"],
                        "title": title,
                        "year": year,
                        "summary": summary,
                    },
                )
            )
        return docs

    def _load_or_build_store(self, force_rebuild: bool = False):
        FAISS, _, _ = self._load_langchain_components()

        with self._lock:
            if self._vector_store is not None and not force_rebuild:
                return self._vector_store

            faiss_dir = self._faiss_dir()
            index_name = self._settings.rag.faiss_index_name
            embeddings = self._embeddings()

            if not force_rebuild:
                try:
                    self._vector_store = FAISS.load_local(
                        str(faiss_dir),
                        embeddings,
                        index_name=index_name,
                        allow_dangerous_deserialization=True,
                    )
                    return self._vector_store
                except Exception:
                    pass

            rows = self._fetch_movie_rows()
            if not rows:
                raise RuntimeError("No movies available to build rag vector index")

            docs = self._build_documents(rows)
            store = FAISS.from_documents(docs, embeddings)
            store.save_local(str(faiss_dir), index_name=index_name)
            self._vector_store = store
            return self._vector_store

    def stream_recommendations(
        self,
        *,
        query: str,
        n: int,
        force_rebuild: bool = False,
    ) -> Iterator[RagMovieItem]:
        store = self._load_or_build_store(force_rebuild=force_rebuild)
        pairs = store.similarity_search_with_score(query, k=int(n))

        seen: set[int] = set()
        emitted = 0
        for doc, score in pairs:
            meta = getattr(doc, "metadata", {}) or {}
            movie_id_raw = meta.get("movie_id")
            if movie_id_raw is None:
                continue

            movie_id = int(movie_id_raw)
            if movie_id in seen:
                continue
            seen.add(movie_id)

            title = str(meta.get("title") or "")
            year = meta.get("year")
            summary = str(meta.get("summary") or "")
            if len(summary) > 300:
                summary = summary[:300] + "..."

            yield RagMovieItem(
                movie_id=movie_id,
                title=title,
                year=int(year) if year is not None else None,
                summary=summary,
                score=float(score) if score is not None else None,
            )
            emitted += 1
            if emitted >= n:
                break


_service: MovieRagService | None = None
_service_lock = threading.RLock()


def get_movie_rag_service(settings: Settings) -> MovieRagService:
    global _service
    with _service_lock:
        if _service is not None:
            return _service

        _service = MovieRagService(settings)
        return _service
