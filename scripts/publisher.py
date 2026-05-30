#!/usr/bin/env python3
"""Publish/edit BYR forum posts.

This is currently intended for test-board exploration before enabling any
WisperTrending production publishing.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from collector import ByrClient, load_env


DEFAULT_BOARD = "test"


class ByrPublisher:
    def __init__(self, client: ByrClient) -> None:
        self.client = client

    def post_thread(self, board: str, subject: str, content: str) -> dict[str, Any]:
        return self._post_json(
            f"/article/{board}/ajax_post.json",
            {
                "subject": subject,
                "content": content,
            },
            referer=f"{self.client.base_url}/#!article/{board}/post",
        )

    def fetch_edit_form(self, board: str, article_id: str) -> str:
        status, text, _ = self.client.request(
            f"/article/{board}/edit/{article_id}",
            referer=f"{self.client.base_url}/#!article/{board}/{article_id}",
        )
        if status != 200:
            raise RuntimeError(f"edit form fetch failed: {status}")
        return text

    def edit_article(self, board: str, article_id: str, subject: str, content: str) -> dict[str, Any]:
        return self._post_json(
            f"/article/{board}/ajax_edit/{article_id}.json",
            {
                "subject": subject,
                "content": content,
            },
            referer=f"{self.client.base_url}/#!article/{board}/edit/{article_id}",
        )

    def _post_json(self, path: str, data: dict[str, str], *, referer: str) -> dict[str, Any]:
        status, text, _ = self.client.request(
            path,
            method="POST",
            data=data,
            accept="application/json, text/javascript, */*; q=0.01",
            referer=referer,
        )
        if status != 200:
            raise RuntimeError(f"POST {path} failed with status {status}")
        return json.loads(text)


def extract_article_id(payload: dict[str, Any]) -> str | None:
    candidates = [payload.get("default"), payload.get("url"), payload.get("ajax_msg")]
    candidates.extend(item.get("url") for item in payload.get("list") or [] if isinstance(item, dict))
    for value in candidates:
        if not value:
            continue
        match = re.search(r"/article/[^/]+/(\d+)", str(value))
        if match:
            return match.group(1)
    return None


def build_test_content(action: str) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return (
        f"WhisperTrending automation {action} test.\n\n"
        f"Time: {now}\n"
        "Purpose: verify the BYR test-board Web publish/edit API.\n"
        "Production publishing should still be gated by explicit scheduling/configuration."
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Publish/edit BYR test-board posts.")
    parser.add_argument("--env", default=".env")
    parser.add_argument("--board", default=DEFAULT_BOARD)
    subparsers = parser.add_subparsers(dest="command", required=True)

    post_parser = subparsers.add_parser("post-test", help="publish a test thread")
    post_parser.add_argument("--subject", default=None)
    post_parser.add_argument("--content", default=None)
    post_parser.add_argument("--content-file", default=None)

    edit_parser = subparsers.add_parser("edit-test", help="edit an existing test thread")
    edit_parser.add_argument("article_id")
    edit_parser.add_argument("--subject", default=None)

    form_parser = subparsers.add_parser("fetch-edit-form", help="fetch edit form HTML")
    form_parser.add_argument("article_id")

    args = parser.parse_args()
    env = load_env(Path(args.env))
    client = ByrClient()
    login_payload = client.login(env["username"], env["password"])
    publisher = ByrPublisher(client)

    if args.command == "post-test":
        subject = args.subject or f"WhisperTrending automation post test {datetime.now():%Y-%m-%d %H:%M:%S}"
        content = args.content
        if args.content_file:
            content = Path(args.content_file).read_text(encoding="utf-8")
        payload = publisher.post_thread(args.board, subject, content or build_test_content("post"))
        print(json.dumps({"login_user": login_payload["id"], "payload": payload, "article_id": extract_article_id(payload)}, ensure_ascii=False, indent=2))
    elif args.command == "edit-test":
        subject = args.subject or f"WhisperTrending automation edit test {datetime.now():%Y-%m-%d %H:%M:%S}"
        payload = publisher.edit_article(args.board, args.article_id, subject, build_test_content("edit"))
        print(json.dumps({"login_user": login_payload["id"], "payload": payload, "article_id": args.article_id}, ensure_ascii=False, indent=2))
    elif args.command == "fetch-edit-form":
        text = publisher.fetch_edit_form(args.board, args.article_id)
        print(text[:4000])
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    raise SystemExit(main())
