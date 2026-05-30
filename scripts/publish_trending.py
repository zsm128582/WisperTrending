#!/usr/bin/env python3
"""Generate and publish/edit the daily WisperTrending forum post."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(SCRIPTS))

from collector import ByrClient, load_env
from publisher import ByrPublisher, extract_article_id
from ranking import (
    DEFAULT_CONFIG_PATH,
    DEFAULT_DB_PATH,
    format_preview_markdown,
    load_config,
    load_ranking_inputs_from_db,
    score_posts,
)
from storage import connect, json_dumps, local_date, parse_datetime


DEFAULT_PUBLISH_BOARD = "Talking"


def markdown_wrapper(content: str) -> str:
    return "[md]\n" + content.rstrip() + "\n[/md]\n"


def subject_for(as_of: str) -> str:
    parsed = parse_datetime(as_of)
    return f"【WhisperTrending】今日悄悄话热度榜 {parsed:%Y-%m-%d}"


def main() -> int:
    parser = argparse.ArgumentParser(description="Publish or update today's WisperTrending post.")
    parser.add_argument("--env", default=".env")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH))
    parser.add_argument("--source-board", default=None, help="board used for ranking; defaults to config board")
    parser.add_argument("--publish-board", default=DEFAULT_PUBLISH_BOARD)
    parser.add_argument("--date", help="YYYY-MM-DD; defaults to latest snapshot date")
    parser.add_argument("--as-of", help="ISO datetime; defaults to latest snapshot")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--preview-out", default="preview.md")
    parser.add_argument("--dry-run", action="store_true", help="generate preview but do not post/edit")
    args = parser.parse_args()

    config = load_config(args.config)
    source_board = args.source_board or config.board
    inputs, as_of = load_ranking_inputs_from_db(
        args.db,
        config,
        board=source_board,
        run_date=args.date,
        as_of=args.as_of,
    )
    if not inputs:
        raise RuntimeError("no ranking snapshots found; run collector first")
    rows = score_posts(inputs, config, as_of=as_of)
    limit = args.limit or config.limit
    preview = format_preview_markdown(
        rows[:limit],
        as_of=as_of,
        window_minutes=config.trend_window_minutes,
    )
    Path(args.preview_out).write_text(preview, encoding="utf-8")

    post_content = markdown_wrapper(preview)
    content_hash = hashlib.sha256(post_content.encode("utf-8")).hexdigest()
    run_date = args.date or local_date(as_of)
    subject = subject_for(as_of)

    with connect(args.db) as storage:
        storage.init_db()
        existing = storage.get_publication_record(
            source_board=source_board,
            publish_board=args.publish_board,
            run_date=run_date,
        )

    if args.dry_run:
        print(
            json_dumps(
                {
                    "dry_run": True,
                    "preview": args.preview_out,
                    "source_board": source_board,
                    "publish_board": args.publish_board,
                    "run_date": run_date,
                    "existing_article_id": existing["article_id"] if existing else None,
                    "subject": subject,
                    "content_hash": content_hash,
                }
            )
        )
        return 0

    env = load_env(Path(args.env))
    client = ByrClient()
    login_payload = client.login(env["username"], env["password"])
    publisher = ByrPublisher(client)

    if existing:
        article_id = str(existing["article_id"])
        payload = publisher.edit_article(args.publish_board, article_id, subject, post_content)
        action = "edit"
    else:
        payload = publisher.post_thread(args.publish_board, subject, post_content)
        article_id = extract_article_id(payload) or ""
        if not article_id:
            raise RuntimeError(f"publish succeeded but article id was not found: {payload}")
        action = "post"

    url = f"https://bbs.byr.cn/article/{args.publish_board}/{article_id}"
    with connect(args.db) as storage:
        storage.init_db()
        record = storage.save_publication_record(
            source_board=source_board,
            publish_board=args.publish_board,
            run_date=run_date,
            article_id=article_id,
            subject=subject,
            url=url,
            content_hash=content_hash,
            action=action,
            metadata={
                "as_of": as_of,
                "login_user": login_payload["id"],
                "payload": payload,
            },
        )

    print(
        json.dumps(
            {
                "action": action,
                "article_id": article_id,
                "url": url,
                "subject": subject,
                "preview": args.preview_out,
                "record": record,
                "payload": payload,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

