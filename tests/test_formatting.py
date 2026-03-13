"""Tests for formatting functions."""

import argparse
import re


from biathlon.formatting import (
    Color,
    format_seconds,
    format_pct,
    rank_style,
    get_output_format,
    is_pretty_output,
    is_markdown_output,
    render_table,
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
    def test_no_format_flag(self):
        args = argparse.Namespace()
        assert is_pretty_output(args) is True
        assert is_markdown_output(args) is False
        assert get_output_format(args) == "pretty"

    def test_tsv_format(self):
        args = argparse.Namespace(format="tsv")
        assert is_pretty_output(args) is False
        assert is_markdown_output(args) is False
        assert get_output_format(args) == "tsv"

    def test_markdown_format(self):
        args = argparse.Namespace(format="markdown")
        assert is_pretty_output(args) is False
        assert is_markdown_output(args) is True
        assert get_output_format(args) == "markdown"


class TestColor:
    def test_muted_uses_rgb_without_dim(self, monkeypatch):
        monkeypatch.setattr(Color, "enabled", classmethod(lambda cls: True))

        out = Color.muted("Muted")

        assert out == "\x1b[38;2;150;150;150mMuted\x1b[0m"
        assert "\x1b[2m" not in out

    def test_muted_red_uses_rgb_without_dim(self, monkeypatch):
        monkeypatch.setattr(Color, "enabled", classmethod(lambda cls: True))

        out = Color.muted_red("(-35)")

        assert out == "\x1b[38;2;196;118;118m(-35)\x1b[0m"
        assert "\x1b[2m" not in out


class TestRenderTableMarkdown:
    def test_markdown_table_basic(self, capsys):
        render_table(
            ["Name", "Score"],
            [["Alice", "10"], ["Bob", "9"]],
            output_format="markdown",
        )
        assert capsys.readouterr().out == (
            "| Name | Score |\n| --- | --- |\n| Alice | 10 |\n| Bob | 9 |\n"
        )

    def test_markdown_escapes_cells_and_strips_ansi(self, capsys):
        def cell_formatter(_value: str, _row_idx: int) -> str:
            return "\033[31mA|B\nC\033[0m"

        render_table(
            ["Col"],
            [["raw"]],
            cell_formatters=[cell_formatter],
            output_format="markdown",
        )
        assert capsys.readouterr().out == ("| Col |\n| --- |\n| A\\|B<br>C |\n")


class TestRenderTablePretty:
    def test_group_headers_inline_renders_on_single_header_line(self, capsys):
        render_table(
            ["Year", "Venue", "Country", "", "", ""],
            [["2022", "Beijing", "China", "NORWAY", "LAEGREID", "FRANCE"]],
            output_format="pretty",
            column_separators={3, 5},
            group_headers=[(3, 5, "GOLD"), (5, 6, "SILVER")],
            group_headers_position="inline",
        )

        out = capsys.readouterr().out
        # Strip ANSI escapes to assert textual layout only.
        clean = re.sub(r"\x1b\[[0-9;]*m", "", out)
        lines = [line for line in clean.splitlines() if line.strip()]

        assert len(lines) >= 3
        assert "Year" in lines[0]
        assert "Venue" in lines[0]
        assert "Country" in lines[0]
        assert "GOLD" in lines[0]
        assert "SILVER" in lines[0]

    def test_row_separators_add_horizontal_rule_between_rows(self, capsys):
        render_table(
            ["Date", "Venue", "Country"],
            [
                ["2025-12-14", "Kontiolahti", "Finland"],
                ["2025-03-01", "Oslo", "Norway"],
            ],
            output_format="pretty",
            column_separators={1},
            row_separators={1},
        )

        out = capsys.readouterr().out
        clean = re.sub(r"\x1b\[[0-9;]*m", "", out)
        lines = [line for line in clean.splitlines() if line.strip()]
        rule_lines = [line for line in lines if "-+-" in line]
        assert len(rule_lines) == 2
