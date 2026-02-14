"""Output formatting utilities for the Biathlon CLI."""

from __future__ import annotations

import os
import re
import sys
import unicodedata
from collections.abc import Callable


class Color:
    """ANSI color codes for terminal output."""

    RESET = "\033[0m"
    DIM = "\033[2m"
    BOLD = "\033[1m"
    OCEAN_BLUE = (79, 193, 255)
    SECTION_TITLE = (255, 170, 0)
    ACCURACY_BANDS = [
        (0.0, 60.0, (176, 0, 32), (176, 0, 32)),  # #B00020
        (60.0, 75.0, (176, 0, 32), (230, 81, 0)),  # #E65100
        (75.0, 85.0, (230, 81, 0), (255, 214, 0)),  # #FFD600
        (85.0, 92.0, (255, 214, 0), (174, 234, 0)),  # #AEEA00
        (92.0, 97.0, (174, 234, 0), (0, 200, 83)),  # #00C853
        (97.0, 100.0, (0, 200, 83), (0, 230, 118)),  # #00E676
    ]
    COLOR_PREFIX = "\033[38;2;"
    COLOR_SUFFIX = "m"

    GOLD = (255, 215, 0)
    LIGHT_GOLD = (218, 165, 32)  # Goldenrod - distinct from bold gold
    SILVER = (192, 192, 192)
    BRONZE = (205, 127, 50)
    FLOWERS = (255, 182, 108)
    OTHER = (215, 215, 215)
    GREEN = (0, 200, 0)
    RED = (220, 60, 60)

    @classmethod
    def enabled(cls) -> bool:
        """Check if colors should be enabled."""
        if os.environ.get("NO_COLOR"):
            return False
        if not sys.stdout.isatty():
            return False
        return True

    @classmethod
    def dim(cls, text: str) -> str:
        """Apply dim style (for past events)."""
        if not cls.enabled():
            return text
        return f"{cls.DIM}{text}{cls.RESET}"

    @classmethod
    def highlight(cls, text: str) -> str:
        """Apply highlight style (for current/next event)."""
        if not cls.enabled():
            return text
        return cls.rgb(text, cls.OCEAN_BLUE, bold=True)

    @classmethod
    def section_title(cls, text: str) -> str:
        """Apply section title style."""
        if not cls.enabled():
            return text
        return cls.rgb(text, cls.SECTION_TITLE, bold=True)

    @classmethod
    def highlight_soft(cls, text: str) -> str:
        """Apply a softer highlight style for related columns."""
        return cls.rgb(text, (0, 170, 0), bold=False)

    @classmethod
    def rgb(cls, text: str, color: tuple[int, int, int], bold: bool = False) -> str:
        """Apply a 24-bit color (optionally bold)."""
        if not cls.enabled():
            return text
        r, g, b = color
        prefix = f"{cls.BOLD}" if bold else ""
        return (
            f"{prefix}{cls.COLOR_PREFIX}{r};{g};{b}{cls.COLOR_SUFFIX}{text}{cls.RESET}"
        )

    @classmethod
    def silver(cls, text: str) -> str:
        """Apply silver style (2nd place)."""
        return cls.rgb(text, cls.SILVER, bold=True)

    @classmethod
    def bronze(cls, text: str) -> str:
        """Apply bronze style (3rd place)."""
        return cls.rgb(text, cls.BRONZE, bold=True)

    @classmethod
    def gold(cls, text: str) -> str:
        """Apply gold style (1st place)."""
        return cls.rgb(text, cls.GOLD, bold=True)

    @classmethod
    def flowers(cls, text: str) -> str:
        """Apply flowers ceremony style (4th/5th/6th place)."""
        return cls.rgb(text, cls.FLOWERS, bold=False)

    @classmethod
    def other(cls, text: str) -> str:
        """Apply style for non-top athletes."""
        return cls.dim(text)

    @classmethod
    def green(cls, text: str, intensity: float = 1.0) -> str:
        """Apply green color with intensity scale (0.0 to 1.0)."""
        base_g = 100
        max_g = 220
        g = int(base_g + (max_g - base_g) * min(1.0, max(0.0, intensity)))
        return cls.rgb(text, (0, g, 0), bold=intensity > 0.5)

    @classmethod
    def red(cls, text: str, intensity: float = 1.0) -> str:
        """Apply red color with intensity scale (0.0 to 1.0)."""
        base_r = 150
        max_r = 240
        r = int(base_r + (max_r - base_r) * min(1.0, max(0.0, intensity)))
        return cls.rgb(text, (r, 50, 50), bold=intensity > 0.5)

    @classmethod
    def accuracy(cls, text: str, pct: float) -> str:
        """Apply color based on accuracy percentage (0.0 to 1.0)."""
        if not cls.enabled():
            return text
        percent = max(0.0, min(100.0, pct * 100.0))
        for low, high, low_color, high_color in cls.ACCURACY_BANDS:
            if percent <= high:
                if high == low:
                    return cls.rgb(text, high_color)
                t = (percent - low) / (high - low)
                color = cls._interp_color(low_color, high_color, t)
                return cls.rgb(text, color)
        return cls.rgb(text, cls.ACCURACY_BANDS[-1][3])

    @classmethod
    def _apply_bands(cls, text: str, value: float, bands: list) -> str:
        """Apply color from a band list based on raw value."""
        if not cls.enabled():
            return text
        for low, high, low_color, high_color in bands:
            if value <= high:
                if high == low:
                    return cls.rgb(text, high_color)
                t = (value - low) / (high - low)
                color = cls._interp_color(low_color, high_color, t)
                return cls.rgb(text, color)
        return cls.rgb(text, bands[-1][3])

    @classmethod
    def clean_race_pct(cls, text: str, pct: float) -> str:
        """Apply color based on clean race percentage (0.0 to 1.0)."""
        return cls._apply_bands(
            text, max(0.0, min(100.0, pct * 100.0)), cls.CLEAN_RACE_PCT_BANDS
        )

    @classmethod
    def clean_stage_pct(cls, text: str, pct: float) -> str:
        """Apply color based on clean stage percentage (0.0 to 1.0)."""
        return cls._apply_bands(
            text, max(0.0, min(100.0, pct * 100.0)), cls.CLEAN_STAGE_PCT_BANDS
        )

    @classmethod
    def shoot_time(cls, text: str, seconds: float) -> str:
        """Apply color based on avg stage shoot time in seconds (lower is better)."""
        return cls._apply_bands(text, seconds, cls.SHOOT_TIME_BANDS)

    @classmethod
    def range_time(cls, text: str, seconds: float) -> str:
        """Apply color based on avg stage range time in seconds (lower is better)."""
        return cls._apply_bands(text, seconds, cls.RANGE_TIME_BANDS)

    # Clean race % bands – tuned for skewed-low distribution (median ~0-8%)
    CLEAN_RACE_PCT_BANDS = [
        (0.0, 5.0, (176, 0, 32), (176, 0, 32)),
        (5.0, 15.0, (176, 0, 32), (230, 81, 0)),
        (15.0, 30.0, (230, 81, 0), (255, 214, 0)),
        (30.0, 45.0, (255, 214, 0), (174, 234, 0)),
        (45.0, 65.0, (174, 234, 0), (0, 200, 83)),
        (65.0, 100.0, (0, 200, 83), (0, 230, 118)),
    ]

    # Clean stage % bands – median ~37%, spread 0-100%
    CLEAN_STAGE_PCT_BANDS = [
        (0.0, 20.0, (176, 0, 32), (176, 0, 32)),
        (20.0, 35.0, (176, 0, 32), (230, 81, 0)),
        (35.0, 50.0, (230, 81, 0), (255, 214, 0)),
        (50.0, 60.0, (255, 214, 0), (174, 234, 0)),
        (60.0, 75.0, (174, 234, 0), (0, 200, 83)),
        (75.0, 100.0, (0, 200, 83), (0, 230, 118)),
    ]

    # Avg stage shoot time bands (seconds, lower = better) – typical 24-37s
    SHOOT_TIME_BANDS = [
        (0.0, 24.0, (0, 230, 118), (0, 230, 118)),
        (24.0, 27.0, (0, 200, 83), (174, 234, 0)),
        (27.0, 30.0, (174, 234, 0), (255, 214, 0)),
        (30.0, 33.0, (255, 214, 0), (230, 81, 0)),
        (33.0, 37.0, (230, 81, 0), (176, 0, 32)),
        (37.0, 999.0, (176, 0, 32), (176, 0, 32)),
    ]

    # Avg stage range time bands (seconds, lower = better) – typical 44-62s
    RANGE_TIME_BANDS = [
        (0.0, 44.0, (0, 230, 118), (0, 230, 118)),
        (44.0, 48.0, (0, 200, 83), (174, 234, 0)),
        (48.0, 52.0, (174, 234, 0), (255, 214, 0)),
        (52.0, 56.0, (255, 214, 0), (230, 81, 0)),
        (56.0, 62.0, (230, 81, 0), (176, 0, 32)),
        (62.0, 999.0, (176, 0, 32), (176, 0, 32)),
    ]

    RELATIVE_BANDS = [
        (0.0, 25.0, (176, 0, 32), (230, 81, 0)),
        (25.0, 50.0, (230, 81, 0), (255, 214, 0)),
        (50.0, 75.0, (255, 214, 0), (174, 234, 0)),
        (75.0, 100.0, (174, 234, 0), (0, 230, 118)),
    ]

    @classmethod
    def relative(cls, text: str, t: float) -> str:
        """Apply color based on a relative 0.0-1.0 scale spread evenly across the range."""
        if not cls.enabled():
            return text
        percent = max(0.0, min(100.0, t * 100.0))
        for low, high, low_color, high_color in cls.RELATIVE_BANDS:
            if percent <= high:
                frac = (percent - low) / (high - low) if high > low else 1.0
                color = cls._interp_color(low_color, high_color, frac)
                return cls.rgb(text, color)
        return cls.rgb(text, cls.RELATIVE_BANDS[-1][3])

    @staticmethod
    def _interp_color(
        low: tuple[int, int, int], high: tuple[int, int, int], t: float
    ) -> tuple[int, int, int]:
        t = max(0.0, min(1.0, t))
        return (
            int(low[0] + (high[0] - low[0]) * t),
            int(low[1] + (high[1] - low[1]) * t),
            int(low[2] + (high[2] - low[2]) * t),
        )


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_ZERO_WIDTH_CODEPOINTS = {0xFE0E, 0xFE0F}


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _display_width(text: str) -> int:
    width = 0
    stripped = _strip_ansi(text)
    idx = 0
    while idx < len(stripped):
        ch = stripped[idx]
        code = ord(ch)
        if code in _ZERO_WIDTH_CODEPOINTS:
            idx += 1
            continue
        if unicodedata.combining(ch):
            idx += 1
            continue
        if unicodedata.category(ch) in {"Mn", "Cf"}:
            idx += 1
            continue
        if unicodedata.east_asian_width(ch) in {"W", "F"}:
            width += 2
        else:
            width += 1
        idx += 1
    return width


def _pad_cell(text: str, width: int, alignment: str = "left") -> str:
    pad_len = width - _display_width(text)
    if pad_len <= 0:
        return text
    if alignment == "right":
        return f"{' ' * pad_len}{text}"
    if alignment == "center":
        left = pad_len // 2
        right = pad_len - left
        return f"{' ' * left}{text}{' ' * right}"
    return f"{text}{' ' * pad_len}"


def render_table(
    headers: list[str],
    rows: list[list[str]],
    pretty: bool,
    row_styles: list[str] | None = None,
    cell_formatters: list[Callable | None] | None = None,
    alignments: list[str] | None = None,
    highlight_headers: list[int] | None = None,
    highlight_header_styles: dict[int, str] | None = None,
    show_headers: bool = True,
    column_separators: set[int] | None = None,
    group_headers: list[tuple[int, int, str]] | None = None,
) -> None:
    """Render tabular data either aligned (pretty) or TSV.

    Args:
        headers: Column headers.
        rows: Data rows.
        pretty: If True, align columns; otherwise output TSV.
        row_styles: Optional list of style names per row ("dim", "highlight", or "").
        cell_formatters: Optional list of functions (one per column) to format cell values.
            Each function takes (value, row_index) and returns formatted string.
        alignments: Optional list of alignment directives ("left", "right", "center") per column.
        highlight_headers: Optional list of column indices to highlight in the header row.
        show_headers: If False, skip printing the header row (default True).
        column_separators: Optional set of column indices before which a vertical separator is drawn.
        group_headers: Optional list of (start_col, end_col, label) tuples to print a
            group header line above the column headers. Each label is centered over
            the span of columns [start_col, end_col).
    """
    if not pretty:
        if show_headers:
            print("\t".join(headers))
        for row in rows:
            print("\t".join(str(cell) for cell in row))
        return

    def apply_row_style(text: str, style: str) -> str:
        if style == "dim":
            return Color.dim(text)
        if style == "highlight":
            return Color.highlight(text)
        if style == "gold":
            return Color.gold(text)
        if style == "silver":
            return Color.silver(text)
        if style == "bronze":
            return Color.bronze(text)
        if style == "flowers":
            return Color.flowers(text)
        if style == "red":
            return Color.red(text)
        if style == "other":
            return Color.other(text)
        return text

    formatted_rows: list[list[str]] = []
    for row_idx, row in enumerate(rows):
        style = ""
        if row_styles and row_idx < len(row_styles):
            style = row_styles[row_idx]

        formatted_row = []
        for col_idx, cell in enumerate(row):
            cell_str = str(cell)
            has_formatter = (
                cell_formatters
                and col_idx < len(cell_formatters)
                and cell_formatters[col_idx]
            )
            formatter = (
                cell_formatters[col_idx] if has_formatter and cell_formatters else None
            )
            if formatter:
                cell_str = formatter(cell_str, row_idx)
            elif style:
                cell_str = apply_row_style(cell_str, style)
            formatted_row.append(cell_str)
        formatted_rows.append(formatted_row)

    widths = [
        max(
            _display_width(str(headers[idx])),
            max((_display_width(row[idx]) for row in formatted_rows), default=0),
        )
        for idx in range(len(headers))
    ]

    sep = column_separators or set()

    def _join(parts: list[str]) -> str:
        if not sep:
            return "  ".join(parts)
        pieces = []
        for i, part in enumerate(parts):
            if i > 0:
                pieces.append(" | " if i in sep else "  ")
            pieces.append(part)
        return "".join(pieces)

    def fmt_row(row_idx: int) -> str:
        parts = []
        for col_idx, cell_str in enumerate(formatted_rows[row_idx]):
            alignment = "left"
            if alignments and col_idx < len(alignments) and alignments[col_idx]:
                alignment = alignments[col_idx]
            parts.append(_pad_cell(cell_str, widths[col_idx], alignment))
        return _join(parts)

    def fmt_header(idx: int, h: str) -> str:
        alignment = "left"
        if alignments and idx < len(alignments) and alignments[idx]:
            alignment = alignments[idx]
        text = _pad_cell(str(h), widths[idx], alignment)
        if highlight_header_styles and idx in highlight_header_styles:
            style = highlight_header_styles[idx]
            if style == "highlight":
                return Color.highlight(text)
            if style == "highlight_soft":
                return Color.highlight_soft(text)
        if highlight_headers and idx in highlight_headers:
            return Color.highlight(text)
        # Bold headers by default
        return f"{Color.BOLD}{text}{Color.RESET}"

    if show_headers:
        if group_headers:
            # Build a group header line above the column headers.
            # Compute position of each column in the rendered line.
            col_positions = []  # (start_char, end_char) for each column
            pos = 0
            for i in range(len(headers)):
                if i > 0:
                    pos += 3 if i in sep else 2  # " | " or "  "
                col_start = pos
                pos += widths[i]
                col_positions.append((col_start, pos))
            line_len = pos
            group_line = [" "] * line_len
            for start_col, end_col, label in group_headers:
                span_start = col_positions[start_col][0]
                span_end = col_positions[end_col - 1][1]
                span_width = span_end - span_start
                # Center the label within the span
                pad = span_width - len(label)
                left_pad = pad // 2
                # Place label characters
                for ci, ch in enumerate(label):
                    group_line[span_start + left_pad + ci] = ch
            raw_line = "".join(group_line).rstrip()
            # Apply bold to the group labels
            for start_col, end_col, label in group_headers:
                raw_line = raw_line.replace(
                    label, f"{Color.BOLD}{label}{Color.RESET}", 1
                )
            print(raw_line)
        header_parts = [fmt_header(i, h) for i, h in enumerate(headers)]
        print(_join(header_parts))
        if sep:
            dash_parts = ["-" * widths[i] for i in range(len(headers))]
            pieces = []
            for i, part in enumerate(dash_parts):
                if i > 0:
                    pieces.append("-+-" if i in sep else "--")
                pieces.append(part)
            print("".join(pieces))
    for idx in range(len(rows)):
        print(fmt_row(idx))


def format_seconds(seconds: float | None) -> str:
    """Render seconds as mm:ss.t or hh:mm:ss.t if needed."""
    if seconds is None:
        return "-"
    hours = int(seconds // 3600)
    remainder = seconds - hours * 3600
    minutes = int(remainder // 60)
    secs = remainder - minutes * 60
    if hours:
        return f"{hours:d}:{minutes:02d}:{secs:04.1f}"
    return f"{minutes:d}:{secs:04.1f}"


def format_pct(numerator: int, denominator: int) -> str:
    """Format a percentage with one decimal place."""
    if denominator == 0:
        return "-"
    return f"{100 * numerator / denominator:.1f}%"


def is_pretty_output(args) -> bool:
    """Return True if output should be pretty-printed (not TSV)."""
    return not getattr(args, "tsv", False)


def rank_style(rank: int | object) -> str:
    """Return style name for a given rank (1-6 get podium/flowers colors)."""
    try:
        r = int(rank)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "other"
    return {
        1: "gold",
        2: "silver",
        3: "bronze",
        4: "flowers",
        5: "flowers",
        6: "flowers",
    }.get(r, "other")
