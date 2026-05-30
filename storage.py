#!/usr/bin/env python3
"""SQLite storage layer for WisperTrending snapshots."""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections.abc import Iterable
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


DEFAULT_DB_PATH = Path("data/wisper_trending.sqlite3")
SCHEMA_VERSION = 1


@dataclass(frozen=True)
class SnapshotRun:
    run_id: int
    board: str
    snapshot_at: str
    post_count: int


class Storage:
    """Persist collection snapshots and query interval deltas."""

    def __init__(self, db_path: str | Path = DEFAULT_DB_PATH) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA synchronous = NORMAL")
        self.conn.create_function("nullable_delta", 2, nullable_delta)

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> Storage:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterable[sqlite3.Connection]:
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def init_db(self) -> None:
        with self.transaction() as conn:
            conn.executescript(SCHEMA_SQL)
            conn.execute(
                """
                INSERT INTO schema_meta (key, value)
                VALUES ('schema_version', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (str(SCHEMA_VERSION),),
            )

    def save_snapshot(
        self,
        posts: Iterable[dict[str, Any]],
        *,
        board: str | None = None,
        source: str = "collector",
        metadata: dict[str, Any] | None = None,
    ) -> SnapshotRun:
        rows = list(posts)
        if not rows:
            raise ValueError("cannot save an empty snapshot")
        inferred_board = board or rows[0].get("board")
        if not inferred_board:
            raise ValueError("board is required when posts do not include board")
        snapshot_at = rows[0].get("snapshot_at") or utc_now_iso()
        snapshot_ts = to_epoch(snapshot_at)
        run_date = local_date(snapshot_at)
        now = utc_now_iso()

        with self.transaction() as conn:
            cursor = conn.execute(
                """
                INSERT INTO snapshot_runs (
                    board, snapshot_at, snapshot_ts, run_date, source,
                    status, started_at, finished_at, post_count, metadata_json
                )
                VALUES (?, ?, ?, ?, ?, 'success', ?, ?, ?, ?)
                """,
                (
                    inferred_board,
                    snapshot_at,
                    snapshot_ts,
                    run_date,
                    source,
                    now,
                    now,
                    len(rows),
                    json_dumps(metadata or {}),
                ),
            )
            run_id = int(cursor.lastrowid)
            for post in rows:
                self._upsert_post(conn, post)
                self._insert_post_snapshot(conn, run_id, snapshot_at, snapshot_ts, run_date, post)
                self._insert_floor_snapshots(conn, run_id, snapshot_at, snapshot_ts, run_date, post)

        return SnapshotRun(
            run_id=run_id,
            board=str(inferred_board),
            snapshot_at=str(snapshot_at),
            post_count=len(rows),
        )

    def import_jsonl(
        self,
        path: str | Path,
        *,
        board: str | None = None,
        source: str = "jsonl_import",
    ) -> list[SnapshotRun]:
        grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
        with Path(path).open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                post = json.loads(line)
                post_board = board or post.get("board")
                snapshot_at = post.get("snapshot_at")
                if not post_board or not snapshot_at:
                    raise ValueError("each JSONL row must include board and snapshot_at")
                grouped.setdefault((str(post_board), str(snapshot_at)), []).append(post)
        return [
            self.save_snapshot(posts, board=group_board, source=source)
            for (group_board, _), posts in sorted(grouped.items())
        ]

    def get_interval_deltas(
        self,
        *,
        board: str,
        start_at: str,
        end_at: str,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Return per-post metric changes between two fixed time boundaries.

        For each post, this uses the earliest snapshot at or after start_at and
        the latest snapshot at or before end_at, which is convenient for
        scheduled collectors that may drift by a few seconds.
        """

        params: list[Any] = [board, to_epoch(start_at), board, board, to_epoch(end_at)]
        limit_sql = ""
        if limit is not None:
            limit_sql = "LIMIT ?"
            params.append(limit)
        rows = self.conn.execute(
            f"""
            WITH start_points AS (
                SELECT ps.*
                FROM post_snapshots ps
                JOIN (
                    SELECT post_id, MIN(snapshot_ts) AS snapshot_ts
                    FROM post_snapshots
                    WHERE board = ? AND snapshot_ts >= ?
                    GROUP BY post_id
                ) chosen
                  ON chosen.post_id = ps.post_id
                 AND chosen.snapshot_ts = ps.snapshot_ts
                WHERE ps.board = ?
            ),
            end_points AS (
                SELECT ps.*
                FROM post_snapshots ps
                JOIN (
                    SELECT post_id, MAX(snapshot_ts) AS snapshot_ts
                    FROM post_snapshots
                    WHERE board = ? AND snapshot_ts <= ?
                    GROUP BY post_id
                ) chosen
                  ON chosen.post_id = ps.post_id
                 AND chosen.snapshot_ts = ps.snapshot_ts
            )
            SELECT
                e.post_id,
                p.title,
                p.url,
                e.board,
                s.snapshot_at AS start_snapshot_at,
                e.snapshot_at AS end_snapshot_at,
                e.reply_count - s.reply_count AS reply_delta,
                nullable_delta(e.view_count, s.view_count) AS view_delta,
                e.total_like_count - s.total_like_count AS like_delta,
                e.total_dislike_count - s.total_dislike_count AS dislike_delta,
                e.reply_count,
                e.view_count,
                e.total_like_count,
                e.total_dislike_count,
                (
                    (e.reply_count - s.reply_count) * 20
                    + (e.total_like_count - s.total_like_count) * 8
                    - (e.total_dislike_count - s.total_dislike_count) * 3
                    + COALESCE(nullable_delta(e.view_count, s.view_count), 0)
                ) AS trend_score
            FROM end_points e
            JOIN start_points s ON s.post_id = e.post_id
            JOIN posts p ON p.post_id = e.post_id
            WHERE e.snapshot_ts > s.snapshot_ts
            ORDER BY trend_score DESC, reply_delta DESC, like_delta DESC
            {limit_sql}
            """,
            params,
        ).fetchall()
        return [dict(row) for row in rows]

    def get_daily_snapshots(self, *, board: str, run_date: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT run_id, board, snapshot_at, post_count, source, status
            FROM snapshot_runs
            WHERE board = ? AND run_date = ?
            ORDER BY snapshot_ts
            """,
            (board, run_date),
        ).fetchall()
        return [dict(row) for row in rows]

    def get_publication_record(
        self,
        *,
        source_board: str,
        publish_board: str,
        run_date: str,
        kind: str = "daily_top",
    ) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            SELECT *
            FROM publication_records
            WHERE source_board = ?
              AND publish_board = ?
              AND run_date = ?
              AND kind = ?
            """,
            (source_board, publish_board, run_date, kind),
        ).fetchone()
        return dict(row) if row else None

    def save_publication_record(
        self,
        *,
        source_board: str,
        publish_board: str,
        run_date: str,
        article_id: str,
        subject: str,
        url: str,
        content_hash: str,
        action: str,
        kind: str = "daily_top",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = utc_now_iso()
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO publication_records (
                    source_board, publish_board, run_date, kind, article_id,
                    subject, url, content_hash, first_published_at,
                    last_published_at, last_action, metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_board, publish_board, run_date, kind)
                DO UPDATE SET
                    article_id = excluded.article_id,
                    subject = excluded.subject,
                    url = excluded.url,
                    content_hash = excluded.content_hash,
                    last_published_at = excluded.last_published_at,
                    last_action = excluded.last_action,
                    metadata_json = excluded.metadata_json
                """,
                (
                    source_board,
                    publish_board,
                    run_date,
                    kind,
                    article_id,
                    subject,
                    url,
                    content_hash,
                    now,
                    now,
                    action,
                    json_dumps(metadata or {}),
                ),
            )
        record = self.get_publication_record(
            source_board=source_board,
            publish_board=publish_board,
            run_date=run_date,
            kind=kind,
        )
        if record is None:
            raise RuntimeError("publication record was not saved")
        return record

    def _upsert_post(self, conn: sqlite3.Connection, post: dict[str, Any]) -> None:
        post_id = required_str(post, "post_id")
        conn.execute(
            """
            INSERT INTO posts (
                post_id, board, title, url, created_at, created_ts, first_seen_at, last_seen_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(post_id) DO UPDATE SET
                board = excluded.board,
                title = excluded.title,
                url = excluded.url,
                created_at = COALESCE(excluded.created_at, posts.created_at),
                created_ts = COALESCE(excluded.created_ts, posts.created_ts),
                last_seen_at = excluded.last_seen_at
            """,
            (
                post_id,
                required_str(post, "board"),
                post.get("title"),
                post.get("url"),
                post.get("created_at"),
                to_epoch_or_none(post.get("created_at")),
                post.get("snapshot_at"),
                post.get("snapshot_at"),
            ),
        )

    def _insert_post_snapshot(
        self,
        conn: sqlite3.Connection,
        run_id: int,
        snapshot_at: str,
        snapshot_ts: int,
        run_date: str,
        post: dict[str, Any],
    ) -> None:
        conn.execute(
            """
            INSERT INTO post_snapshots (
                run_id, post_id, board, snapshot_at, snapshot_ts, run_date,
                last_reply_at, last_reply_ts, reply_count, view_count,
                root_like_count, root_dislike_count, reply_like_count,
                reply_dislike_count, total_like_count, total_dislike_count,
                reaction_floor_count, raw_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id, post_id) DO UPDATE SET
                last_reply_at = excluded.last_reply_at,
                last_reply_ts = excluded.last_reply_ts,
                reply_count = excluded.reply_count,
                view_count = excluded.view_count,
                root_like_count = excluded.root_like_count,
                root_dislike_count = excluded.root_dislike_count,
                reply_like_count = excluded.reply_like_count,
                reply_dislike_count = excluded.reply_dislike_count,
                total_like_count = excluded.total_like_count,
                total_dislike_count = excluded.total_dislike_count,
                reaction_floor_count = excluded.reaction_floor_count,
                raw_json = excluded.raw_json
            """,
            (
                run_id,
                required_str(post, "post_id"),
                required_str(post, "board"),
                snapshot_at,
                snapshot_ts,
                run_date,
                post.get("last_reply_at"),
                to_epoch_or_none(post.get("last_reply_at")),
                int_or_default(post.get("reply_count"), 0),
                int_or_none(post.get("view_count")),
                int_or_default(post.get("root_like_count"), 0),
                int_or_default(post.get("root_dislike_count"), 0),
                int_or_default(post.get("reply_like_count"), 0),
                int_or_default(post.get("reply_dislike_count"), 0),
                int_or_default(post.get("total_like_count"), 0),
                int_or_default(post.get("total_dislike_count"), 0),
                int_or_default(post.get("reaction_floor_count"), 0),
                json_dumps(post),
            ),
        )

    def _insert_floor_snapshots(
        self,
        conn: sqlite3.Connection,
        run_id: int,
        snapshot_at: str,
        snapshot_ts: int,
        run_date: str,
        post: dict[str, Any],
    ) -> None:
        post_id = required_str(post, "post_id")
        board = required_str(post, "board")
        for position, floor in enumerate(post.get("reaction_floors") or []):
            floor_article_id = str(floor.get("article_id") or f"{post_id}:{position}")
            conn.execute(
                """
                INSERT INTO article_floors (
                    floor_article_id, post_id, board, floor_label, floor_index,
                    author, is_root, first_seen_at, last_seen_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(floor_article_id) DO UPDATE SET
                    post_id = excluded.post_id,
                    board = excluded.board,
                    floor_label = excluded.floor_label,
                    floor_index = excluded.floor_index,
                    author = excluded.author,
                    is_root = excluded.is_root,
                    last_seen_at = excluded.last_seen_at
                """,
                (
                    floor_article_id,
                    post_id,
                    board,
                    floor.get("floor"),
                    position,
                    floor.get("author"),
                    1 if floor.get("is_root") else 0,
                    snapshot_at,
                    snapshot_at,
                ),
            )
            conn.execute(
                """
                INSERT INTO floor_snapshots (
                    run_id, floor_article_id, post_id, board, snapshot_at,
                    snapshot_ts, run_date, like_count, dislike_count, raw_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id, floor_article_id) DO UPDATE SET
                    like_count = excluded.like_count,
                    dislike_count = excluded.dislike_count,
                    raw_json = excluded.raw_json
                """,
                (
                    run_id,
                    floor_article_id,
                    post_id,
                    board,
                    snapshot_at,
                    snapshot_ts,
                    run_date,
                    int_or_default(floor.get("like_count"), 0),
                    int_or_default(floor.get("dislike_count"), 0),
                    json_dumps(floor),
                ),
            )


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS snapshot_runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    board TEXT NOT NULL,
    snapshot_at TEXT NOT NULL,
    snapshot_ts INTEGER NOT NULL,
    run_date TEXT NOT NULL,
    source TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'success',
    started_at TEXT NOT NULL,
    finished_at TEXT,
    post_count INTEGER NOT NULL DEFAULT 0,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE(board, snapshot_at, source)
);

CREATE TABLE IF NOT EXISTS posts (
    post_id TEXT PRIMARY KEY,
    board TEXT NOT NULL,
    title TEXT,
    url TEXT,
    created_at TEXT,
    created_ts INTEGER,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS post_snapshots (
    snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES snapshot_runs(run_id) ON DELETE CASCADE,
    post_id TEXT NOT NULL REFERENCES posts(post_id) ON DELETE CASCADE,
    board TEXT NOT NULL,
    snapshot_at TEXT NOT NULL,
    snapshot_ts INTEGER NOT NULL,
    run_date TEXT NOT NULL,
    last_reply_at TEXT,
    last_reply_ts INTEGER,
    reply_count INTEGER NOT NULL DEFAULT 0,
    view_count INTEGER,
    root_like_count INTEGER NOT NULL DEFAULT 0,
    root_dislike_count INTEGER NOT NULL DEFAULT 0,
    reply_like_count INTEGER NOT NULL DEFAULT 0,
    reply_dislike_count INTEGER NOT NULL DEFAULT 0,
    total_like_count INTEGER NOT NULL DEFAULT 0,
    total_dislike_count INTEGER NOT NULL DEFAULT 0,
    reaction_floor_count INTEGER NOT NULL DEFAULT 0,
    raw_json TEXT NOT NULL,
    UNIQUE(run_id, post_id)
);

CREATE TABLE IF NOT EXISTS article_floors (
    floor_article_id TEXT PRIMARY KEY,
    post_id TEXT NOT NULL REFERENCES posts(post_id) ON DELETE CASCADE,
    board TEXT NOT NULL,
    floor_label TEXT,
    floor_index INTEGER,
    author TEXT,
    is_root INTEGER NOT NULL DEFAULT 0,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS floor_snapshots (
    floor_snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES snapshot_runs(run_id) ON DELETE CASCADE,
    floor_article_id TEXT NOT NULL REFERENCES article_floors(floor_article_id) ON DELETE CASCADE,
    post_id TEXT NOT NULL REFERENCES posts(post_id) ON DELETE CASCADE,
    board TEXT NOT NULL,
    snapshot_at TEXT NOT NULL,
    snapshot_ts INTEGER NOT NULL,
    run_date TEXT NOT NULL,
    like_count INTEGER NOT NULL DEFAULT 0,
    dislike_count INTEGER NOT NULL DEFAULT 0,
    raw_json TEXT NOT NULL,
    UNIQUE(run_id, floor_article_id)
);

CREATE TABLE IF NOT EXISTS publication_records (
    publication_id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_board TEXT NOT NULL,
    publish_board TEXT NOT NULL,
    run_date TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'daily_top',
    article_id TEXT NOT NULL,
    subject TEXT NOT NULL,
    url TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    first_published_at TEXT NOT NULL,
    last_published_at TEXT NOT NULL,
    last_action TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE(source_board, publish_board, run_date, kind)
);

CREATE INDEX IF NOT EXISTS idx_runs_board_date
    ON snapshot_runs(board, run_date, snapshot_ts);
CREATE INDEX IF NOT EXISTS idx_post_snapshots_board_time
    ON post_snapshots(board, snapshot_ts);
CREATE INDEX IF NOT EXISTS idx_post_snapshots_post_time
    ON post_snapshots(post_id, snapshot_ts);
CREATE INDEX IF NOT EXISTS idx_post_snapshots_date_score
    ON post_snapshots(board, run_date, total_like_count, reply_count);
CREATE INDEX IF NOT EXISTS idx_floor_snapshots_post_time
    ON floor_snapshots(post_id, snapshot_ts);
CREATE INDEX IF NOT EXISTS idx_floor_snapshots_board_date
    ON floor_snapshots(board, run_date, snapshot_ts);
CREATE INDEX IF NOT EXISTS idx_publication_records_date
    ON publication_records(source_board, publish_board, run_date);
"""


def connect(db_path: str | Path = DEFAULT_DB_PATH) -> Storage:
    return Storage(db_path)


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def required_str(row: dict[str, Any], key: str) -> str:
    value = row.get(key)
    if value is None or value == "":
        raise ValueError(f"missing required field: {key}")
    return str(value)


def int_or_none(value: Any) -> int | None:
    if value is None or value == "":
        return None
    return int(value)


def int_or_default(value: Any, default: int) -> int:
    if value is None or value == "":
        return default
    return int(value)


def parse_datetime(value: str) -> datetime:
    normalized = value.replace("Z", "+00:00")
    return datetime.fromisoformat(normalized)


def to_epoch(value: str) -> int:
    return int(parse_datetime(value).timestamp())


def to_epoch_or_none(value: Any) -> int | None:
    if not value:
        return None
    return to_epoch(str(value))


def local_date(value: str) -> str:
    return parse_datetime(value).date().isoformat()


def utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def nullable_delta(end_value: int | None, start_value: int | None) -> int | None:
    if end_value is None or start_value is None:
        return None
    return int(end_value) - int(start_value)


def load_jsonl(path: str | Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with Path(path).open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="Manage WisperTrending SQLite storage.")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="SQLite database path")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init", help="initialize database schema")

    import_parser = subparsers.add_parser("import-jsonl", help="import collector JSONL snapshots")
    import_parser.add_argument("path", help="JSONL file to import")
    import_parser.add_argument("--board")

    runs_parser = subparsers.add_parser("runs", help="list snapshot runs for a board/date")
    runs_parser.add_argument("--board", required=True)
    runs_parser.add_argument("--date", required=True)

    deltas_parser = subparsers.add_parser("deltas", help="query interval deltas")
    deltas_parser.add_argument("--board", required=True)
    deltas_parser.add_argument("--start", required=True)
    deltas_parser.add_argument("--end", required=True)
    deltas_parser.add_argument("--limit", type=int, default=10)

    args = parser.parse_args()
    with connect(args.db) as storage:
        storage.init_db()
        if args.command == "init":
            print(json_dumps({"db": args.db, "schema_version": SCHEMA_VERSION}))
        elif args.command == "import-jsonl":
            runs = storage.import_jsonl(args.path, board=args.board)
            print(json_dumps({"imported_runs": [run.__dict__ for run in runs]}))
        elif args.command == "runs":
            print(json_dumps(storage.get_daily_snapshots(board=args.board, run_date=args.date)))
        elif args.command == "deltas":
            print(
                json_dumps(
                    storage.get_interval_deltas(
                        board=args.board,
                        start_at=args.start,
                        end_at=args.end,
                        limit=args.limit,
                    )
                )
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
