from __future__ import annotations

import unittest

from ranking import (
    ComponentWeights,
    FreshnessConfig,
    MetricWeights,
    RankingConfig,
    RankingInput,
    score_post,
    score_posts,
)


CONFIG = RankingConfig(
    board="IWhisper",
    timezone="Asia/Shanghai",
    trend_window_minutes=30,
    limit=10,
    components=ComponentWeights(
        current_heat_weight=0.5,
        growth_speed_weight=0.4,
        freshness_weight=0.1,
    ),
    current_heat=MetricWeights(
        reply_weight=8,
        like_weight=5,
        dislike_weight=-2,
        view_weight=0.2,
    ),
    growth_speed=MetricWeights(
        reply_weight=30,
        like_weight=14,
        dislike_weight=-4,
        view_weight=1,
    ),
    freshness=FreshnessConfig(
        max_bonus=100,
        half_life_minutes=120,
        cutoff_minutes=720,
    ),
)

AS_OF = "2026-05-30T12:00:00+08:00"


def post(**overrides):
    data = {
        "post_id": "p",
        "title": "title",
        "url": None,
        "board": "IWhisper",
        "created_at": "2026-05-30T08:00:00+08:00",
        "snapshot_at": AS_OF,
        "reply_count": 0,
        "total_like_count": 0,
        "total_dislike_count": 0,
        "view_count": None,
        "baseline_snapshot_at": "2026-05-30T11:30:00+08:00",
        "baseline_reply_count": 0,
        "baseline_total_like_count": 0,
        "baseline_total_dislike_count": 0,
        "baseline_view_count": None,
    }
    data.update(overrides)
    return RankingInput(**data)


class RankingTests(unittest.TestCase):
    def test_new_post_gets_freshness_bonus(self):
        new = post(post_id="new", created_at="2026-05-30T11:55:00+08:00")
        old = post(post_id="old", created_at="2026-05-30T01:00:00+08:00")

        scored = score_posts([old, new], CONFIG, as_of=AS_OF)

        self.assertEqual(scored[0]["post_id"], "new")
        self.assertGreater(scored[0]["freshness_bonus"], scored[1]["freshness_bonus"])

    def test_old_post_can_win_with_high_total_heat(self):
        old_hot = post(
            post_id="old-hot",
            created_at="2026-05-30T01:00:00+08:00",
            reply_count=60,
            total_like_count=80,
        )
        fresh_quiet = post(
            post_id="fresh-quiet",
            created_at="2026-05-30T11:58:00+08:00",
            reply_count=1,
            total_like_count=0,
        )

        scored = score_posts([fresh_quiet, old_hot], CONFIG, as_of=AS_OF)

        self.assertEqual(scored[0]["post_id"], "old-hot")
        self.assertGreater(scored[0]["current_heat_score"], scored[1]["current_heat_score"])

    def test_no_growth_has_zero_growth_score(self):
        row = score_post(
            post(
                reply_count=5,
                total_like_count=8,
                baseline_reply_count=5,
                baseline_total_like_count=8,
            ),
            CONFIG,
            as_of_dt=__import__("storage").parse_datetime(AS_OF),
        )

        self.assertEqual(row["reply_delta"], 0)
        self.assertEqual(row["like_delta"], 0)
        self.assertEqual(row["growth_speed_score"], 0)

    def test_sudden_growth_boosts_growth_score(self):
        quiet = post(post_id="quiet", reply_count=20, total_like_count=20, baseline_reply_count=20, baseline_total_like_count=20)
        burst = post(post_id="burst", reply_count=20, total_like_count=20, baseline_reply_count=5, baseline_total_like_count=2)

        scored = score_posts([quiet, burst], CONFIG, as_of=AS_OF)

        self.assertEqual(scored[0]["post_id"], "burst")
        self.assertGreater(scored[0]["growth_speed_score"], scored[1]["growth_speed_score"])

    def test_missing_baseline_is_treated_as_no_growth(self):
        row = score_post(
            post(
                baseline_snapshot_at=None,
                baseline_reply_count=None,
                baseline_total_like_count=None,
                baseline_total_dislike_count=None,
                reply_count=5,
                total_like_count=9,
            ),
            CONFIG,
            as_of_dt=__import__("storage").parse_datetime(AS_OF),
        )

        self.assertFalse(row["has_baseline"])
        self.assertEqual(row["reply_delta"], 0)
        self.assertEqual(row["like_delta"], 0)
        self.assertGreater(row["current_heat_score"], 0)


if __name__ == "__main__":
    unittest.main()

