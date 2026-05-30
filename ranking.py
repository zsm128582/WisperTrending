#!/usr/bin/env python3
"""Ranking algorithm for WisperTrending daily top posts."""

from __future__ import annotations

import argparse
import json
import math
import tomllib
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from storage import connect, json_dumps, parse_datetime, to_epoch


DEFAULT_CONFIG_PATH = Path("config/ranking.toml")
DEFAULT_DB_PATH = Path("data/wisper_trending.sqlite3")


@dataclass(frozen=True)
class MetricWeights:
    reply_weight: float
    like_weight: float
    dislike_weight: float
    view_weight: float


@dataclass(frozen=True)
class ComponentWeights:
    current_heat_weight: float
    growth_speed_weight: float
    freshness_weight: float


@dataclass(frozen=True)
class FreshnessConfig:
    max_bonus: float
    half_life_minutes: float
    cutoff_minutes: float


@dataclass(frozen=True)
class RankingConfig:
    board: str
    timezone: str
    trend_window_minutes: int
    limit: int
    components: ComponentWeights
    current_heat: MetricWeights
    growth_speed: MetricWeights
    freshness: FreshnessConfig


@dataclass(frozen=True)
class RankingInput:
    post_id: str
    title: str
    url: str | None
    board: str
    created_at: str | None
    snapshot_at: str
    reply_count: int
    total_like_count: int
    total_dislike_count: int
    view_count: int | None = None
    baseline_snapshot_at: str | None = None
    baseline_reply_count: int | None = None
    baseline_total_like_count: int | None = None
    baseline_total_dislike_count: int | None = None
    baseline_view_count: int | None = None


def load_config(path: str | Path = DEFAULT_CONFIG_PATH) -> RankingConfig:
    payload = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    ranking = payload["ranking"]
    return RankingConfig(
        board=ranking.get("board", "IWhisper"),
        timezone=ranking.get("timezone", "Asia/Shanghai"),
        trend_window_minutes=int(ranking.get("trend_window_minutes", 30)),
        limit=int(ranking.get("limit", 10)),
        components=ComponentWeights(**ranking["components"]),
        current_heat=MetricWeights(**ranking["current_heat"]),
        growth_speed=MetricWeights(**ranking["growth_speed"]),
        freshness=FreshnessConfig(**ranking["freshness"]),
    )


def score_posts(
    posts: list[RankingInput],
    config: RankingConfig,
    *,
    as_of: str | None = None,
) -> list[dict[str, Any]]:
    as_of_dt = parse_datetime(as_of or max(post.snapshot_at for post in posts))
    scored = [score_post(post, config, as_of_dt=as_of_dt) for post in posts]
    return sorted(
        scored,
        key=lambda item: (
            item["final_score"],
            item["growth_speed_score"],
            item["current_heat_score"],
            item["created_at"] or "",
        ),
        reverse=True,
    )


def score_post(post: RankingInput, config: RankingConfig, *, as_of_dt: datetime) -> dict[str, Any]:
    reply_delta = nonnegative_delta(post.reply_count, post.baseline_reply_count)
    like_delta = nonnegative_delta(post.total_like_count, post.baseline_total_like_count)
    dislike_delta = nonnegative_delta(post.total_dislike_count, post.baseline_total_dislike_count)
    view_delta = nullable_nonnegative_delta(post.view_count, post.baseline_view_count)

    current_heat_score = weighted_metric_score(
        config.current_heat,
        replies=post.reply_count,
        likes=post.total_like_count,
        dislikes=post.total_dislike_count,
        views=post.view_count,
    )
    growth_speed_score = weighted_metric_score(
        config.growth_speed,
        replies=reply_delta,
        likes=like_delta,
        dislikes=dislike_delta,
        views=view_delta,
    )
    freshness_bonus = compute_freshness_bonus(post.created_at, as_of_dt, config.freshness)
    final_score = (
        current_heat_score * config.components.current_heat_weight
        + growth_speed_score * config.components.growth_speed_weight
        + freshness_bonus * config.components.freshness_weight
    )

    return {
        "post_id": post.post_id,
        "title": post.title,
        "url": post.url,
        "board": post.board,
        "created_at": post.created_at,
        "snapshot_at": post.snapshot_at,
        "baseline_snapshot_at": post.baseline_snapshot_at,
        "reply_count": post.reply_count,
        "total_like_count": post.total_like_count,
        "total_dislike_count": post.total_dislike_count,
        "view_count": post.view_count,
        "reply_delta": reply_delta,
        "like_delta": like_delta,
        "dislike_delta": dislike_delta,
        "view_delta": view_delta,
        "current_heat_score": round(current_heat_score, 3),
        "growth_speed_score": round(growth_speed_score, 3),
        "freshness_bonus": round(freshness_bonus, 3),
        "final_score": round(final_score, 3),
        "has_baseline": post.baseline_snapshot_at is not None,
    }


def weighted_metric_score(
    weights: MetricWeights,
    *,
    replies: int | None,
    likes: int | None,
    dislikes: int | None,
    views: int | None,
) -> float:
    return (
        int_or_zero(replies) * weights.reply_weight
        + int_or_zero(likes) * weights.like_weight
        + int_or_zero(dislikes) * weights.dislike_weight
        + int_or_zero(views) * weights.view_weight
    )


def compute_freshness_bonus(
    created_at: str | None,
    as_of_dt: datetime,
    config: FreshnessConfig,
) -> float:
    if not created_at:
        return 0.0
    age_minutes = (as_of_dt - parse_datetime(created_at)).total_seconds() / 60
    if age_minutes < 0:
        age_minutes = 0
    if age_minutes > config.cutoff_minutes:
        return 0.0
    if config.half_life_minutes <= 0:
        return config.max_bonus
    return config.max_bonus * math.pow(0.5, age_minutes / config.half_life_minutes)


def nonnegative_delta(current: int | None, baseline: int | None) -> int:
    if baseline is None:
        return 0
    return max(0, int_or_zero(current) - int_or_zero(baseline))


def nullable_nonnegative_delta(current: int | None, baseline: int | None) -> int | None:
    if current is None or baseline is None:
        return None
    return max(0, int(current) - int(baseline))


def int_or_zero(value: int | None) -> int:
    return 0 if value is None else int(value)


def load_ranking_inputs_from_db(
    db_path: str | Path,
    config: RankingConfig,
    *,
    board: str | None = None,
    run_date: str | None = None,
    as_of: str | None = None,
) -> tuple[list[RankingInput], str]:
    board = board or config.board
    tz = ZoneInfo(config.timezone)
    as_of_dt = parse_datetime(as_of) if as_of else None
    with connect(db_path) as storage:
        storage.init_db()
        if as_of_dt is None:
            row = storage.conn.execute(
                """
                SELECT MAX(snapshot_ts) AS snapshot_ts
                FROM post_snapshots
                WHERE board = ? AND (? IS NULL OR run_date = ?)
                """,
                (board, run_date, run_date),
            ).fetchone()
            if row is None or row["snapshot_ts"] is None:
                return [], ""
            as_of_dt = datetime.fromtimestamp(row["snapshot_ts"], tz)
        if run_date is None:
            run_date = as_of_dt.astimezone(tz).date().isoformat()
        current_ts = int(as_of_dt.timestamp())
        baseline_ts = int((as_of_dt - timedelta(minutes=config.trend_window_minutes)).timestamp())
        day_start = datetime.fromisoformat(run_date).replace(tzinfo=tz)
        day_end = day_start + timedelta(days=1)
        rows = storage.conn.execute(
            """
            WITH current_points AS (
                SELECT ps.*
                FROM post_snapshots ps
                JOIN (
                    SELECT post_id, MAX(snapshot_ts) AS snapshot_ts
                    FROM post_snapshots
                    WHERE board = ? AND run_date = ? AND snapshot_ts <= ?
                    GROUP BY post_id
                ) chosen
                  ON chosen.post_id = ps.post_id
                 AND chosen.snapshot_ts = ps.snapshot_ts
            ),
            baseline_points AS (
                SELECT ps.*
                FROM post_snapshots ps
                JOIN (
                    SELECT post_id, MIN(snapshot_ts) AS snapshot_ts
                    FROM post_snapshots
                    WHERE board = ? AND run_date = ? AND snapshot_ts >= ? AND snapshot_ts < ?
                    GROUP BY post_id
                ) chosen
                  ON chosen.post_id = ps.post_id
                 AND chosen.snapshot_ts = ps.snapshot_ts
            )
            SELECT
                c.post_id,
                p.title,
                p.url,
                c.board,
                p.created_at,
                c.snapshot_at,
                c.reply_count,
                c.total_like_count,
                c.total_dislike_count,
                c.view_count,
                b.snapshot_at AS baseline_snapshot_at,
                b.reply_count AS baseline_reply_count,
                b.total_like_count AS baseline_total_like_count,
                b.total_dislike_count AS baseline_total_dislike_count,
                b.view_count AS baseline_view_count
            FROM current_points c
            JOIN posts p ON p.post_id = c.post_id
            LEFT JOIN baseline_points b ON b.post_id = c.post_id
            WHERE p.created_ts >= ? AND p.created_ts < ?
            """,
            (
                board,
                run_date,
                current_ts,
                board,
                run_date,
                baseline_ts,
                current_ts,
                int(day_start.timestamp()),
                int(day_end.timestamp()),
            ),
        ).fetchall()
    inputs = [
        RankingInput(
            post_id=str(row["post_id"]),
            title=row["title"] or "",
            url=row["url"],
            board=row["board"],
            created_at=row["created_at"],
            snapshot_at=row["snapshot_at"],
            reply_count=int(row["reply_count"] or 0),
            total_like_count=int(row["total_like_count"] or 0),
            total_dislike_count=int(row["total_dislike_count"] or 0),
            view_count=row["view_count"],
            baseline_snapshot_at=row["baseline_snapshot_at"],
            baseline_reply_count=row["baseline_reply_count"],
            baseline_total_like_count=row["baseline_total_like_count"],
            baseline_total_dislike_count=row["baseline_total_dislike_count"],
            baseline_view_count=row["baseline_view_count"],
        )
        for row in rows
    ]
    return inputs, as_of_dt.isoformat()


def format_top10(rows: list[dict[str, Any]], *, as_of: str, window_minutes: int) -> str:
    return format_preview_markdown(rows, as_of=as_of, window_minutes=window_minutes)


def format_preview_markdown(rows: list[dict[str, Any]], *, as_of: str, window_minutes: int) -> str:
    display_time = format_display_time(as_of)
    lines = [f"【WhisperTrending】今日悄悄话热度榜 {display_time}", ""]
    lines.extend(
        [
            "统计说明：",
            "本榜单基于今日“悄悄话”板块公开可见的帖子列表与站内互动数据生成。",
            f"排序综合考虑当前热度、近 {window_minutes} 分钟增长速度与轻微发帖时间加成。",
            "仅展示标题、链接和聚合指标，不展示发帖人信息或匿名正文。",
            "",
        ]
    )
    for index, row in enumerate(rows[:10], start=1):
        reply_text = blue(f"回复 {row['reply_count']}")
        like_text = green(f"赞 {row['total_like_count']}")
        dislike_text = red(f"踩 {row['total_dislike_count']}")
        reply_delta_text = blue(f"+{row['reply_delta']} 回复")
        like_delta_text = green(f"+{row['like_delta']} 赞")
        dislike_delta_text = red(f"+{row['dislike_delta']} 踩")
        lines.append(f"{index}. {row['title']}")
        lines.append("   " f"{reply_text} / {like_text} / {dislike_text}")
        lines.append(
            "   "
            f"近{window_minutes}分钟 "
            f"{reply_delta_text} "
            f"{like_delta_text} "
            f"{dislike_delta_text}"
        )
        lines.append(
            "   "
            f"综合 {row['final_score']:.1f} / "
            f"热度 {row['current_heat_score']:.1f} / "
            f"增长 {row['growth_speed_score']:.1f}"
        )
        if row.get("url"):
            lines.append(f"   链接：{row['url']}")
        lines.append("")
    lines.extend(
        [
            "如有帖子已删除、锁定或不适合传播，请以原帖状态为准；本帖只做站内趋势索引，不保存或展示匿名正文。",
        ]
    )
    return "\n".join(lines).rstrip() + "\n"


def format_display_time(value: str) -> str:
    parsed = parse_datetime(value)
    return parsed.strftime("%Y-%m-%d %H:%M")


def color(text: str, value: str) -> str:
    return f"[color={value}]{text}[/color]"


def blue(text: str) -> str:
    return color(text, "#1f6feb")


def green(text: str) -> str:
    return color(text, "#2da44e")


def red(text: str) -> str:
    return color(text, "#cf222e")


def main() -> int:
    parser = argparse.ArgumentParser(description="Rank WisperTrending daily top posts.")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH))
    parser.add_argument("--board")
    parser.add_argument("--date", help="YYYY-MM-DD; defaults to latest snapshot date")
    parser.add_argument("--as-of", help="ISO datetime; defaults to latest snapshot")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--preview-out", help="write forum markdown preview to this path")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of forum text")
    args = parser.parse_args()

    config = load_config(args.config)
    inputs, as_of = load_ranking_inputs_from_db(
        args.db,
        config,
        board=args.board,
        run_date=args.date,
        as_of=args.as_of,
    )
    rows = score_posts(inputs, config, as_of=as_of)
    limit = args.limit or config.limit
    payload = {
        "as_of": as_of,
        "board": args.board or config.board,
        "trend_window_minutes": config.trend_window_minutes,
        "count": len(rows),
        "top": rows[:limit],
    }
    preview = format_preview_markdown(
        rows[:limit],
        as_of=as_of,
        window_minutes=config.trend_window_minutes,
    )
    if args.preview_out:
        Path(args.preview_out).write_text(preview, encoding="utf-8")
    if args.json:
        print(json_dumps(payload))
    else:
        print(preview, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
