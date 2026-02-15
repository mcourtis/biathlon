"""Tests for CLI parser output format flags."""

import pytest

from biathlon.cli import build_parser


def test_results_accepts_format_tsv():
    parser = build_parser()
    args = parser.parse_args(["results", "--format", "tsv"])
    assert args.format == "tsv"


def test_events_accepts_format_markdown():
    parser = build_parser()
    args = parser.parse_args(["events", "--format", "markdown"])
    assert args.format == "markdown"


def test_invalid_format_rejected():
    parser = build_parser()
    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["events", "--format", "json"])
    assert exc_info.value.code == 2


def test_tsv_flag_rejected():
    parser = build_parser()
    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["events", "--tsv"])
    assert exc_info.value.code == 2
