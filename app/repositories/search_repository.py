from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import logging
from time import perf_counter
from typing import Any

from sqlalchemy import bindparam, text
from sqlalchemy.exc import SQLAlchemyError

from app.reco.recall.two_tower.db import get_engine


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SearchPage:
    total: int
    results: list[dict[str, Any]]
    strategy: str


class SearchRepository:
    def __init__(self, mysql_dsn: str | None) -> None:
        self._mysql_dsn = mysql_dsn

    def search(
        self,
        *,
        query: str,
        n: int,
        offset: int,
        tag_ids: list[int],
        sort_by: str,
        sort_order: str,
        release_start_date: date | None,
        release_end_date: date | None,
        duration_min: int | None,
        duration_max: int | None,
    ) -> SearchPage:
        normalized_query = str(query or "").strip()
        if not normalized_query:
            return self._search_browse(
                n=n,
                offset=offset,
                tag_ids=tag_ids,
                sort_by=sort_by,
                sort_order=sort_order,
                release_start_date=release_start_date,
                release_end_date=release_end_date,
                duration_min=duration_min,
                duration_max=duration_max,
            )

        try:
            fulltext_page = self._search_fulltext(
                query=normalized_query,
                n=n,
                offset=offset,
                tag_ids=tag_ids,
                sort_by=sort_by,
                sort_order=sort_order,
                release_start_date=release_start_date,
                release_end_date=release_end_date,
                duration_min=duration_min,
                duration_max=duration_max,
            )
        except SQLAlchemyError:
            logger.exception("Search fulltext failed, fallback to like query, query_len=%s", len(normalized_query))
            fulltext_page = None

        if fulltext_page is not None and int(fulltext_page.total) > 0:
            return fulltext_page

        return self._search_like(
            query=normalized_query,
            n=n,
            offset=offset,
            tag_ids=tag_ids,
            sort_by=sort_by,
            sort_order=sort_order,
            release_start_date=release_start_date,
            release_end_date=release_end_date,
            duration_min=duration_min,
            duration_max=duration_max,
        )

    def _search_browse(
        self,
        *,
        n: int,
        offset: int,
        tag_ids: list[int],
        sort_by: str,
        sort_order: str,
        release_start_date: date | None,
        release_end_date: date | None,
        duration_min: int | None,
        duration_max: int | None,
    ) -> SearchPage:
        params, join_sql, where_sql = self._build_common_filters(
            tag_ids=tag_ids,
            release_start_date=release_start_date,
            release_end_date=release_end_date,
            duration_min=duration_min,
            duration_max=duration_max,
        )
        params["n"] = int(n)
        params["offset"] = int(offset)

        candidate_sql = text(
            f"""
            SELECT
              m.movie_id,
              0.0 AS relevance_score
            FROM movie m
            {join_sql}
            WHERE {where_sql}
            ORDER BY {self._build_order_sql(sort_by=sort_by, sort_order=sort_order, score_alias=None)}
            LIMIT :n OFFSET :offset
            """
        )
        count_sql = text(
            f"""
            SELECT COUNT(1) AS total
            FROM movie m
            {join_sql}
            WHERE {where_sql}
            """
        )
        return self._execute_search(
            strategy="browse",
            params=params,
            candidate_sql=self._bind_tag_ids(candidate_sql, tag_ids=tag_ids),
            count_sql=self._bind_tag_ids(count_sql, tag_ids=tag_ids),
        )

    def _search_fulltext(
        self,
        *,
        query: str,
        n: int,
        offset: int,
        tag_ids: list[int],
        sort_by: str,
        sort_order: str,
        release_start_date: date | None,
        release_end_date: date | None,
        duration_min: int | None,
        duration_max: int | None,
    ) -> SearchPage:
        params, join_sql, where_sql = self._build_common_filters(
            tag_ids=tag_ids,
            release_start_date=release_start_date,
            release_end_date=release_end_date,
            duration_min=duration_min,
            duration_max=duration_max,
        )
        params["query_text"] = query
        params["n"] = int(n)
        params["offset"] = int(offset)
        score_expr = "MATCH(m.title, m.summary) AGAINST (:query_text IN NATURAL LANGUAGE MODE)"

        candidate_sql = text(
            f"""
            SELECT
              m.movie_id,
              {score_expr} AS relevance_score
            FROM movie m
            {join_sql}
            WHERE {where_sql}
              AND {score_expr} > 0
            ORDER BY {self._build_order_sql(sort_by=sort_by, sort_order=sort_order, score_alias='relevance_score')}
            LIMIT :n OFFSET :offset
            """
        )
        count_sql = text(
            f"""
            SELECT COUNT(1) AS total
            FROM movie m
            {join_sql}
            WHERE {where_sql}
              AND {score_expr} > 0
            """
        )
        return self._execute_search(
            strategy="fulltext",
            params=params,
            candidate_sql=self._bind_tag_ids(candidate_sql, tag_ids=tag_ids),
            count_sql=self._bind_tag_ids(count_sql, tag_ids=tag_ids),
        )

    def _search_like(
        self,
        *,
        query: str,
        n: int,
        offset: int,
        tag_ids: list[int],
        sort_by: str,
        sort_order: str,
        release_start_date: date | None,
        release_end_date: date | None,
        duration_min: int | None,
        duration_max: int | None,
    ) -> SearchPage:
        params, join_sql, where_sql = self._build_common_filters(
            tag_ids=tag_ids,
            release_start_date=release_start_date,
            release_end_date=release_end_date,
            duration_min=duration_min,
            duration_max=duration_max,
        )
        query_params, query_clause, score_expr = self._build_like_query_parts(query=query)
        params.update(query_params)
        params["n"] = int(n)
        params["offset"] = int(offset)

        candidate_sql = text(
            f"""
            SELECT
              m.movie_id,
              {score_expr} AS relevance_score
            FROM movie m
            {join_sql}
            WHERE {where_sql}
              AND {query_clause}
            ORDER BY {self._build_order_sql(sort_by=sort_by, sort_order=sort_order, score_alias='relevance_score')}
            LIMIT :n OFFSET :offset
            """
        )
        count_sql = text(
            f"""
            SELECT COUNT(1) AS total
            FROM movie m
            {join_sql}
            WHERE {where_sql}
              AND {query_clause}
            """
        )
        return self._execute_search(
            strategy="like",
            params=params,
            candidate_sql=self._bind_tag_ids(candidate_sql, tag_ids=tag_ids),
            count_sql=self._bind_tag_ids(count_sql, tag_ids=tag_ids),
        )

    def _execute_search(
        self,
        *,
        strategy: str,
        params: dict[str, Any],
        candidate_sql,
        count_sql,
    ) -> SearchPage:
        engine = get_engine(self._mysql_dsn)
        if engine is None:
            raise RuntimeError("search_mysql_engine_unavailable")

        detail_sql = text(
            """
            SELECT
              m.movie_id,
              m.title,
              m.year,
              m.release_date,
              m.duration_min,
              m.poster,
              COALESCE(m.summary, '') AS summary,
              COALESCE(m.rating_sum, 0) AS rating_sum,
              COALESCE(m.rating_count, 0) AS rating_count,
                            COALESCE(m.bayesian_rating, 0) AS bayesian_rating,
                            COALESCE(m.collect_count, 0) AS collect_count
            FROM movie m
            WHERE m.movie_id IN :movie_ids
              AND m.status = 'published'
              AND m.deleted_at IS NULL
            """
        ).bindparams(bindparam("movie_ids", expanding=True))

        started = perf_counter()
        with engine.connect() as conn:
            total = int(conn.execute(count_sql, params).scalar_one())
            candidate_rows = conn.execute(candidate_sql, params).mappings().all()
            movie_ids = [int(row["movie_id"]) for row in candidate_rows if row.get("movie_id") is not None]
            if not movie_ids:
                logger.info(
                    "Search repository completed, strategy=%s, total=%s, returned=0, elapsed_ms=%.2f",
                    strategy,
                    total,
                    (perf_counter() - started) * 1000.0,
                )
                return SearchPage(total=total, results=[], strategy=strategy)

            detail_rows = conn.execute(detail_sql, {"movie_ids": movie_ids}).mappings().all()

        score_by_movie_id = {
            int(row["movie_id"]): float(row.get("relevance_score") or 0.0)
            for row in candidate_rows
            if row.get("movie_id") is not None
        }
        detail_by_movie_id = {
            int(row["movie_id"]): row
            for row in detail_rows
            if row.get("movie_id") is not None
        }

        results: list[dict[str, Any]] = []
        for movie_id in movie_ids:
            row = detail_by_movie_id.get(int(movie_id))
            if row is None:
                continue
            summary = str(row.get("summary") or "").strip()
            if len(summary) > 300:
                summary = summary[:300] + "..."

            rating_count = max(int(row.get("rating_count") or 0), 0)
            rating_sum = float(row.get("rating_sum") or 0.0)
            rating_avg = (rating_sum / float(rating_count)) if rating_count > 0 else None

            results.append(
                {
                    "movie_id": int(row["movie_id"]),
                    "title": str(row.get("title") or ""),
                    "year": int(row["year"]) if row.get("year") is not None else None,
                    "release_date": str(row["release_date"]) if row.get("release_date") is not None else None,
                    "duration_min": int(row["duration_min"]) if row.get("duration_min") is not None else None,
                    "poster": str(row.get("poster") or ""),
                    "summary": summary,
                    "rating_avg": rating_avg,
                    "rating_count": rating_count,
                    "bayesian_rating": float(row.get("bayesian_rating") or 0.0),
                    "collect_count": max(int(row.get("collect_count") or 0), 0),
                    "score": float(score_by_movie_id.get(int(movie_id), 0.0)),
                }
            )

        logger.info(
            "Search repository completed, strategy=%s, total=%s, returned=%s, elapsed_ms=%.2f",
            strategy,
            total,
            len(results),
            (perf_counter() - started) * 1000.0,
        )
        return SearchPage(total=total, results=results, strategy=strategy)

    def _build_common_filters(
        self,
        *,
        tag_ids: list[int],
        release_start_date: date | None,
        release_end_date: date | None,
        duration_min: int | None,
        duration_max: int | None,
    ) -> tuple[dict[str, Any], str, str]:
        params: dict[str, Any] = {}
        join_parts: list[str] = []
        where_clauses = [
            "m.status = 'published'",
            "m.deleted_at IS NULL",
        ]

        if tag_ids:
            params["tag_ids"] = list(tag_ids)
            join_parts.append(
                """
                INNER JOIN (
                    SELECT DISTINCT mt.movie_id
                    FROM movie_tag mt
                    WHERE mt.tag_id IN :tag_ids
                ) tag_filter ON tag_filter.movie_id = m.movie_id
                """
            )

        if release_start_date is not None:
            params["release_start_date"] = release_start_date
            where_clauses.append("m.release_date >= :release_start_date")
        if release_end_date is not None:
            params["release_end_date"] = release_end_date
            where_clauses.append("m.release_date <= :release_end_date")
        if duration_min is not None:
            params["duration_min"] = int(duration_min)
            where_clauses.append("m.duration_min >= :duration_min")
        if duration_max is not None:
            params["duration_max"] = int(duration_max)
            where_clauses.append("m.duration_min <= :duration_max")

        join_sql = "\n".join(part.strip() for part in join_parts)
        where_sql = " AND ".join(where_clauses)
        return params, join_sql, where_sql

    def _build_like_query_parts(self, *, query: str) -> tuple[dict[str, Any], str, str]:
        params: dict[str, Any] = {"query_like": f"%{query}%"}
        token_terms: list[str] = []
        token_scores: list[str] = []
        for index, token in enumerate(self._tokenize_query(query)):
            pname = f"kw_{index}"
            params[pname] = f"%{token}%"
            token_terms.append(f"(m.title LIKE :{pname} OR m.summary LIKE :{pname})")
            token_scores.append(
                f"CASE WHEN m.title LIKE :{pname} THEN 2 "
                f"WHEN m.summary LIKE :{pname} THEN 1 ELSE 0 END"
            )

        query_clause = "(m.title LIKE :query_like OR m.summary LIKE :query_like)"
        if token_terms:
            query_clause = f"({query_clause} OR ({' AND '.join(token_terms)}))"

        score_parts = [
            "CASE WHEN m.title LIKE :query_like THEN 10 ELSE 0 END",
            "CASE WHEN m.summary LIKE :query_like THEN 4 ELSE 0 END",
            "LEAST(COALESCE(m.rating_count, 0), 10000) / 10000.0",
        ]
        score_parts.extend(token_scores)
        return params, query_clause, " + ".join(score_parts)

    def _build_order_sql(self, *, sort_by: str, sort_order: str, score_alias: str | None) -> str:
        secondary: list[str] = []
        if score_alias:
            secondary.append(f"{score_alias} DESC")
        secondary.append("COALESCE(m.rating_count, 0) DESC")
        secondary.append("m.movie_id DESC")

        if sort_by == "rating":
            return ", ".join([f"COALESCE(m.bayesian_rating, 0) {sort_order}", *secondary])
        if sort_by == "collect":
            return ", ".join([f"COALESCE(m.collect_count, 0) {sort_order}", *secondary])
        if sort_by == "duration":
            return ", ".join([
                "(m.duration_min IS NULL) ASC",
                f"COALESCE(m.duration_min, 0) {sort_order}",
                *secondary,
            ])
        if sort_by == "time":
            return ", ".join([
                "(m.release_date IS NULL) ASC",
                f"m.release_date {sort_order}",
                *secondary,
            ])
        if score_alias:
            return ", ".join([f"{score_alias} {sort_order}", "COALESCE(m.rating_count, 0) DESC", "m.movie_id DESC"])
        return "m.movie_id DESC"

    def _bind_tag_ids(self, stmt, *, tag_ids: list[int]):
        if not tag_ids:
            return stmt
        return stmt.bindparams(bindparam("tag_ids", expanding=True))

    def _tokenize_query(self, query: str) -> list[str]:
        tokens = [token.strip() for token in str(query or "").split() if token.strip()]
        return list(dict.fromkeys(tokens))