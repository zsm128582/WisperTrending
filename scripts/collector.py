#!/usr/bin/env python3
"""Read-only BYR board collector for WisperTrending."""

from __future__ import annotations

import argparse
import http.cookiejar
import html
import json
import math
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable
from zoneinfo import ZoneInfo


BASE_URL = "https://bbs.byr.cn"
DEFAULT_BOARD = "IWhisper"
TIMEZONE = ZoneInfo("Asia/Shanghai")


def load_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        raise RuntimeError(f"missing env file: {path}")
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


class ByrClient:
    def __init__(self, base_url: str = BASE_URL) -> None:
        self.base_url = base_url.rstrip("/")
        self.cookie_jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cookie_jar)
        )

    def request(
        self,
        path: str,
        *,
        method: str = "GET",
        data: dict[str, str] | None = None,
        accept: str = "text/html, */*; q=0.01",
        referer: str = BASE_URL + "/",
    ) -> tuple[int, str, str]:
        url = path if path.startswith("http") else self.base_url + path
        body = None
        if data is not None:
            body = urllib.parse.urlencode(data).encode("utf-8")
        headers = {
            "Accept": accept,
            "Referer": referer,
            "User-Agent": "WisperTrending/0.1 (+https://bbs.byr.cn/)",
            "X-Requested-With": "XMLHttpRequest",
        }
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with self.opener.open(req, timeout=20) as response:
                raw = response.read()
                charset = response.headers.get_content_charset() or "gbk"
                return response.status, raw.decode(charset, errors="replace"), url
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            charset = exc.headers.get_content_charset() or "gbk"
            text = raw.decode(charset, errors="replace")
            raise RuntimeError(f"HTTP {exc.code} for {url}: {text[:200]}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"network error for {url}: {exc.reason}") from exc

    def login(self, username: str, password: str) -> dict:
        status, text, _ = self.request(
            "/user/ajax_login.json",
            method="POST",
            data={"id": username, "passwd": password, "s-mode": "0", "CookieDate": "2"},
            accept="application/json, text/javascript, */*; q=0.01",
        )
        if status != 200:
            raise RuntimeError(f"login failed with status {status}")
        payload = json.loads(text)
        if payload.get("ajax_st") != 1 or not payload.get("is_login"):
            msg = payload.get("ajax_msg") or "unknown login failure"
            raise RuntimeError(f"login failed: {msg}")
        return payload

    def fetch_board(self, board: str, page: int) -> str:
        suffix = f"?p={page}" if page > 1 else ""
        status, text, _ = self.request(
            f"/board/{urllib.parse.quote(board)}{suffix}",
            referer=f"{self.base_url}/#!board/{urllib.parse.quote(board)}",
        )
        if status != 200:
            raise RuntimeError(f"board fetch failed with status {status}")
        return text

    def fetch_article(self, board: str, post_id: str, page: int = 1) -> str:
        suffix = f"?p={page}" if page > 1 else ""
        status, text, _ = self.request(
            f"/article/{urllib.parse.quote(board)}/{urllib.parse.quote(post_id)}{suffix}",
            referer=f"{self.base_url}/#!article/{urllib.parse.quote(board)}/{urllib.parse.quote(post_id)}",
        )
        if status != 200:
            raise RuntimeError(f"article fetch failed with status {status}")
        return text


@dataclass
class Cell:
    classes: set[str] = field(default_factory=set)
    text_parts: list[str] = field(default_factory=list)
    links: list[tuple[str, str]] = field(default_factory=list)
    current_link: str | None = None
    current_link_text: list[str] | None = None

    @property
    def text(self) -> str:
        return clean_text("".join(self.text_parts))


@dataclass
class Row:
    classes: set[str] = field(default_factory=set)
    cells: list[Cell] = field(default_factory=list)


class BoardParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.in_board_table = False
        self.table_depth = 0
        self.in_tbody = False
        self.current_row: Row | None = None
        self.current_cell: Cell | None = None
        self.rows: list[Row] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = dict(attrs)
        classes = set((attr.get("class") or "").split())
        if tag == "table" and "board-list" in classes:
            self.in_board_table = True
            self.table_depth = 1
            return
        if self.in_board_table and tag == "table":
            self.table_depth += 1
        if not self.in_board_table:
            return
        if tag == "tbody":
            self.in_tbody = True
        elif tag == "tr" and self.in_tbody:
            self.current_row = Row(classes=classes)
        elif tag == "td" and self.current_row is not None:
            self.current_cell = Cell(classes=classes)
        elif tag == "a" and self.current_cell is not None:
            self.current_cell.current_link = attr.get("href")
            self.current_cell.current_link_text = []

    def handle_endtag(self, tag: str) -> None:
        if not self.in_board_table:
            return
        if tag == "a" and self.current_cell is not None:
            href = self.current_cell.current_link
            link_text = clean_text("".join(self.current_cell.current_link_text or []))
            if href:
                self.current_cell.links.append((href, link_text))
            self.current_cell.current_link = None
            self.current_cell.current_link_text = None
        elif tag == "td" and self.current_row is not None and self.current_cell is not None:
            self.current_row.cells.append(self.current_cell)
            self.current_cell = None
        elif tag == "tr" and self.current_row is not None:
            self.rows.append(self.current_row)
            self.current_row = None
        elif tag == "tbody":
            self.in_tbody = False
        elif tag == "table":
            self.table_depth -= 1
            if self.table_depth <= 0:
                self.in_board_table = False

    def handle_data(self, data: str) -> None:
        if self.current_cell is not None:
            self.current_cell.text_parts.append(data)
            if self.current_cell.current_link_text is not None:
                self.current_cell.current_link_text.append(data)


@dataclass
class ArticleFloor:
    floor: str | None = None
    author: str | None = None
    article_id: str | None = None
    is_root: bool = False
    like_count: int | None = None
    dislike_count: int | None = None
    text_parts: list[str] = field(default_factory=list)


class ArticleParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.floors: list[ArticleFloor] = []
        self.current_floor: ArticleFloor | None = None
        self.a_wrap_depth = 0
        self.capture_field: str | None = None
        self.capture_parts: list[str] = []
        self.current_reaction: str | None = None
        self.current_reaction_href: str | None = None
        self.current_reaction_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = dict(attrs)
        classes = set((attr.get("class") or "").split())
        if tag == "div" and "a-wrap" in classes:
            self.current_floor = ArticleFloor()
            self.a_wrap_depth = 1
            return
        if self.current_floor is None:
            return
        if tag == "div":
            self.a_wrap_depth += 1
        if tag == "span" and "a-u-name" in classes:
            self.capture_field = "author"
            self.capture_parts = []
        elif tag == "span" and "a-pos" in classes:
            self.capture_field = "floor"
            self.capture_parts = []
        elif tag == "a":
            reaction = reaction_kind(classes)
            if reaction:
                self.current_reaction = reaction
                self.current_reaction_href = attr.get("href")
                self.current_reaction_parts = []

    def handle_endtag(self, tag: str) -> None:
        if self.current_floor is None:
            return
        if tag == "span" and self.capture_field:
            setattr(self.current_floor, self.capture_field, clean_text("".join(self.capture_parts)))
            self.capture_field = None
            self.capture_parts = []
        elif tag == "a" and self.current_reaction:
            text = clean_text("".join(self.current_reaction_parts))
            count = parse_reaction_count(text)
            article_id = article_id_from_href(self.current_reaction_href or "")
            if article_id and not self.current_floor.article_id:
                self.current_floor.article_id = article_id
            if self.current_reaction in {"support", "like"} and self.current_floor.like_count is None:
                self.current_floor.like_count = count
            elif self.current_reaction in {"oppose", "dislike"} and self.current_floor.dislike_count is None:
                self.current_floor.dislike_count = count
            if self.current_reaction in {"support", "oppose"}:
                self.current_floor.is_root = True
            self.current_reaction = None
            self.current_reaction_href = None
            self.current_reaction_parts = []
        elif tag == "div":
            self.a_wrap_depth -= 1
            if self.a_wrap_depth <= 0:
                self.floors.append(self.current_floor)
                self.current_floor = None

    def handle_data(self, data: str) -> None:
        if self.current_floor is None:
            return
        self.current_floor.text_parts.append(data)
        if self.capture_field:
            self.capture_parts.append(data)
        if self.current_reaction:
            self.current_reaction_parts.append(data)


def clean_text(value: str) -> str:
    value = html.unescape(value)
    value = value.replace("\xa0", " ").replace("\u2003", " ")
    return re.sub(r"\s+", " ", value).strip()


def reaction_kind(classes: set[str]) -> str | None:
    if "a-func-support" in classes:
        return "support"
    if "a-func-oppose" in classes:
        return "oppose"
    if "a-func-like" in classes:
        return "like"
    if "a-func-cai" in classes:
        return "dislike"
    return None


def parse_reaction_count(text: str) -> int:
    match = re.search(r"\(([+-]?\d+)\)", text)
    return int(match.group(1)) if match else 0


def article_id_from_href(href: str) -> str | None:
    match = re.search(r"/article/[^/]+/(?:ajax_[^/]+/)?(\d+)\.json", href)
    if match:
        return match.group(1)
    match = re.search(r"/article/[^/]+/post/(\d+)", href)
    return match.group(1) if match else None


def parse_forum_datetime(value: str, today: datetime) -> datetime | None:
    text = clean_text(value)
    if not text:
        return None
    if re.fullmatch(r"\d{2}:\d{2}:\d{2}", text):
        hour, minute, second = map(int, text.split(":"))
        return today.replace(hour=hour, minute=minute, second=second, microsecond=0)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        date = datetime.strptime(text, "%Y-%m-%d")
        return date.replace(tzinfo=TIMEZONE)
    return None


def first_cell(row: Row, class_name: str) -> Cell | None:
    return next((cell for cell in row.cells if class_name in cell.classes), None)


def cells(row: Row, class_name: str) -> list[Cell]:
    return [cell for cell in row.cells if class_name in cell.classes]


def parse_board_posts(html_text: str, board: str, snapshot_at: datetime) -> list[dict]:
    parser = BoardParser()
    parser.feed(html_text)
    posts: list[dict] = []
    today_start = snapshot_at.replace(hour=0, minute=0, second=0, microsecond=0)
    for row in parser.rows:
        if "top" in row.classes:
            continue
        title_cell = first_cell(row, "title_9")
        reply_cell = first_cell(row, "title_11")
        time_cells = cells(row, "title_10")
        if not title_cell or not reply_cell or len(time_cells) < 2:
            continue
        article_link = next(
            (
                (href, title)
                for href, title in title_cell.links
                if href.startswith(f"/article/{board}/")
            ),
            None,
        )
        if not article_link:
            continue
        href, title = article_link
        match = re.search(r"/article/[^/]+/(\d+)", href)
        if not match:
            continue
        created_at = parse_forum_datetime(time_cells[0].text, snapshot_at)
        last_reply_at = parse_forum_datetime(time_cells[1].text, snapshot_at)
        try:
            reply_count = int(re.search(r"\d+", reply_cell.text).group(0))
        except AttributeError:
            reply_count = 0
        post_id = match.group(1)
        posts.append(
            {
                "post_id": post_id,
                "title": title,
                "url": f"{BASE_URL}/article/{board}/{post_id}",
                "board": board,
                "created_at": created_at.isoformat() if created_at else None,
                "last_reply_at": last_reply_at.isoformat() if last_reply_at else None,
                "reply_count": reply_count,
                "view_count": None,
                "snapshot_at": snapshot_at.isoformat(),
                "is_created_today": bool(created_at and created_at >= today_start),
            }
        )
    return posts


def parse_article_reaction_floors(html_text: str) -> list[dict]:
    parser = ArticleParser()
    parser.feed(html_text)
    return [
        {
            "floor": floor.floor,
            "author": floor.author,
            "article_id": floor.article_id,
            "is_root": floor.is_root,
            "like_count": floor.like_count or 0,
            "dislike_count": floor.dislike_count or 0,
        }
        for floor in parser.floors
    ]


def summarize_reaction_floors(floors: list[dict]) -> dict:
    seen_ids: set[str] = set()
    unique_floors: list[dict] = []
    for floor in floors:
        article_id = floor.get("article_id")
        dedupe_key = article_id or f"{floor.get('floor')}:{floor.get('author')}:{len(unique_floors)}"
        if dedupe_key in seen_ids:
            continue
        seen_ids.add(dedupe_key)
        unique_floors.append(floor)
    root = next((floor for floor in floors if floor["is_root"]), floors[0] if floors else None)
    reply_floors = [floor for floor in unique_floors if floor is not root]
    root_like_count = root["like_count"] if root else 0
    root_dislike_count = root["dislike_count"] if root else 0
    reply_like_count = sum(floor["like_count"] for floor in reply_floors)
    reply_dislike_count = sum(floor["dislike_count"] for floor in reply_floors)
    return {
        "root_like_count": root_like_count,
        "root_dislike_count": root_dislike_count,
        "reply_like_count": reply_like_count,
        "reply_dislike_count": reply_dislike_count,
        "total_like_count": root_like_count + reply_like_count,
        "total_dislike_count": root_dislike_count + reply_dislike_count,
        "reaction_floor_count": len(unique_floors),
        "reaction_floors": unique_floors,
    }


def collect_today(
    client: ByrClient,
    board: str,
    *,
    max_pages: int = 0,
    page_delay: float = 0.0,
    sleep_notice: bool = False,
) -> list[dict]:
    snapshot_at = datetime.now(TIMEZONE)
    today_start = snapshot_at.replace(hour=0, minute=0, second=0, microsecond=0)
    page = 1
    all_posts: list[dict] = []
    while True:
        html_text = client.fetch_board(board, page)
        posts = parse_board_posts(html_text, board, snapshot_at)
        if not posts:
            break
        all_posts.extend(post for post in posts if post["is_created_today"])
        last_reply_values = [
            datetime.fromisoformat(post["last_reply_at"])
            for post in posts
            if post.get("last_reply_at")
        ]
        if last_reply_values and max(last_reply_values) < today_start:
            break
        if max_pages and page >= max_pages:
            break
        page += 1
        if page_delay > 0:
            time.sleep(page_delay)
        if sleep_notice:
            print(f"fetched page {page - 1}, continuing...", file=sys.stderr)
    return all_posts


def enrich_reactions(
    client: ByrClient,
    posts: list[dict],
    *,
    article_delay: float = 0.0,
    max_articles: int = 0,
    reaction_pages: str = "all",
) -> None:
    selected_posts = posts[:max_articles] if max_articles else posts
    for index, post in enumerate(selected_posts):
        reply_count = int(post.get("reply_count") or 0)
        page_count = 1 if reaction_pages == "first" else max(1, math.ceil((reply_count + 1) / 10))
        floors: list[dict] = []
        for page in range(1, page_count + 1):
            html_text = client.fetch_article(post["board"], post["post_id"], page=page)
            floors.extend(parse_article_reaction_floors(html_text))
            if article_delay > 0 and (page < page_count or index < len(selected_posts) - 1):
                time.sleep(article_delay)
        post.update(summarize_reaction_floors(floors))


def append_jsonl(path: Path, rows: Iterable[dict]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
            count += 1
    return count


def main() -> int:
    parser = argparse.ArgumentParser(description="Collect BYR board topic snapshots.")
    parser.add_argument("--env", default=".env", help="path to .env with username/password")
    parser.add_argument("--board", default=DEFAULT_BOARD)
    parser.add_argument("--max-pages", type=int, default=0, help="0 means until non-today pages")
    parser.add_argument("--page-delay", type=float, default=0.0, help="seconds to wait between pages")
    parser.add_argument("--out", default="data/snapshots.jsonl")
    parser.add_argument(
        "--sqlite-db",
        help="optional SQLite database path; when set, snapshots are saved to SQLite",
    )
    parser.add_argument("--sqlite-source", default="collector", help="source label for SQLite runs")
    parser.add_argument(
        "--include-reactions",
        action="store_true",
        help="fetch each article page and parse like/dislike counts",
    )
    parser.add_argument(
        "--article-delay",
        type=float,
        default=0.0,
        help="seconds to wait between article reaction fetches",
    )
    parser.add_argument(
        "--max-articles",
        type=int,
        default=0,
        help="limit article reaction fetches; 0 means all collected posts",
    )
    parser.add_argument(
        "--reaction-pages",
        choices=("all", "first"),
        default="all",
        help="fetch all article pages or only the first page when parsing reactions",
    )
    parser.add_argument("--dry-run", action="store_true", help="print summary only")
    args = parser.parse_args()

    env = load_env(Path(args.env))
    username = env.get("username")
    password = env.get("password")
    if not username or not password:
        raise RuntimeError(".env must contain username and password")

    client = ByrClient()
    login_payload = client.login(username, password)
    posts = collect_today(client, args.board, max_pages=args.max_pages, page_delay=args.page_delay)
    if args.include_reactions:
        enrich_reactions(
            client,
            posts,
            article_delay=args.article_delay,
            max_articles=args.max_articles,
            reaction_pages=args.reaction_pages,
        )

    saved_run = None
    if not args.dry_run and args.sqlite_db:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from storage import connect

        with connect(args.sqlite_db) as storage:
            storage.init_db()
            saved_run = storage.save_snapshot(posts, board=args.board, source=args.sqlite_source)

    if not args.dry_run:
        append_jsonl(Path(args.out), posts)

    print(
        json.dumps(
            {
                "board": args.board,
                "login_user": login_payload.get("id"),
                "posts": len(posts),
                "output": None if args.dry_run else args.out,
                "sqlite_run": None if saved_run is None else saved_run.__dict__,
                "sample": posts[:3],
                "view_count_available": any(p.get("view_count") is not None for p in posts),
                "reaction_count_available": any(
                    p.get("total_like_count") is not None or p.get("total_dislike_count") is not None
                    for p in posts
                ),
                "reaction_nonzero_posts": sum(
                    1
                    for p in posts
                    if (p.get("total_like_count") or 0) > 0
                    or (p.get("total_dislike_count") or 0) > 0
                ),
                "reaction_top_sample": [
                    {
                        "post_id": p["post_id"],
                        "title": p["title"],
                        "total_like_count": p.get("total_like_count", 0),
                        "total_dislike_count": p.get("total_dislike_count", 0),
                        "reaction_floor_count": p.get("reaction_floor_count", 0),
                    }
                    for p in sorted(
                        posts,
                        key=lambda item: (
                            (item.get("total_like_count") or 0)
                            + (item.get("total_dislike_count") or 0)
                        ),
                        reverse=True,
                    )[:3]
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
