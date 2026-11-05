"""Tests for the release helpers."""

from __future__ import annotations

from datetime import UTC, datetime

import release


def test_the_first_tag_of_a_month_is_1() -> None:
    assert release.next_tag([], datetime(2026, 11, 5, tzinfo=UTC)) == "2026.11.1"
    assert release.next_tag(["2026.10.4"], datetime(2026, 11, 5, tzinfo=UTC)) == "2026.11.1"


def test_tags_count_up_within_a_month() -> None:
    tags = ["2026.11.1", "2026.11.2", "2026.11.10", "v0.1.0", "2026.1.3"]
    assert release.next_tag(tags, datetime(2026, 11, 30, tzinfo=UTC)) == "2026.11.11"
    assert release.next_tag(tags, datetime(2027, 1, 1, tzinfo=UTC)) == "2027.1.1"
