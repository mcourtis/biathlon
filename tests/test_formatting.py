"""Tests for formatting functions."""

import argparse


from biathlon.formatting import (
    format_seconds,
    format_pct,
    rank_style,
    is_pretty_output,
)


class TestFormatSeconds:
    def test_minutes_seconds(self):
        assert format_seconds(90.5) == "1:30.5"

    def test_hours_minutes_seconds(self):
        assert format_seconds(3661.5) == "1:01:01.5"

    def test_zero(self):
        assert format_seconds(0) == "0:00.0"

    def test_none(self):
        assert format_seconds(None) == "-"

    def test_sub_minute(self):
        assert format_seconds(45.3) == "0:45.3"


class TestFormatPct:
    def test_basic_percentage(self):
        assert format_pct(3, 4) == "75.0%"

    def test_full_percentage(self):
        assert format_pct(10, 10) == "100.0%"

    def test_zero_numerator(self):
        assert format_pct(0, 10) == "0.0%"

    def test_zero_denominator(self):
        assert format_pct(5, 0) == "-"


class TestRankStyle:
    def test_gold(self):
        assert rank_style(1) == "gold"

    def test_silver(self):
        assert rank_style(2) == "silver"

    def test_bronze(self):
        assert rank_style(3) == "bronze"

    def test_fourth(self):
        assert rank_style(4) == "flowers"

    def test_fifth(self):
        assert rank_style(5) == "flowers"

    def test_sixth(self):
        assert rank_style(6) == "flowers"

    def test_seventh(self):
        assert rank_style(7) == "other"

    def test_string_rank(self):
        assert rank_style("1") == "gold"

    def test_invalid_rank(self):
        assert rank_style("invalid") == "other"

    def test_none_rank(self):
        assert rank_style(None) == "other"


class TestIsPrettyOutput:
    def test_no_tsv_flag(self):
        args = argparse.Namespace()
        assert is_pretty_output(args) is True

    def test_tsv_false(self):
        args = argparse.Namespace(tsv=False)
        assert is_pretty_output(args) is True

    def test_tsv_true(self):
        args = argparse.Namespace(tsv=True)
        assert is_pretty_output(args) is False
