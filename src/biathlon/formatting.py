"""Output formatting utilities for the Biathlon CLI."""

from __future__ import annotations

import os
import sys
from collections.abc import Callable


class Color:
    """ANSI color codes for terminal output."""

    RESET = "\033[0m"
    DIM = "\033[2m"
    BOLD = "\033[1m"
    OCEAN_BLUE = (79, 193, 255)
    SECTION_TITLE = (255, 170, 0)
    ACCURACY_BANDS = [
        (0.0, 60.0, (176, 0, 32), (176, 0, 32)),       # #B00020
        (60.0, 75.0, (176, 0, 32), (230, 81, 0)),       # #E65100
        (75.0, 85.0, (230, 81, 0), (255, 214, 0)),      # #FFD600
        (85.0, 92.0, (255, 214, 0), (174, 234, 0)),     # #AEEA00
        (92.0, 97.0, (174, 234, 0), (0, 200, 83)),      # #00C853
        (97.0, 100.0, (0, 200, 83), (0, 230, 118)),     # #00E676
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
        return f"{prefix}{cls.COLOR_PREFIX}{r};{g};{b}{cls.COLOR_SUFFIX}{text}{cls.RESET}"

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

    @staticmethod
    def _interp_color(low: tuple[int, int, int], high: tuple[int, int, int], t: float) -> tuple[int, int, int]:
        t = max(0.0, min(1.0, t))
        return (
            int(low[0] + (high[0] - low[0]) * t),
            int(low[1] + (high[1] - low[1]) * t),
            int(low[2] + (high[2] - low[2]) * t),
        )


def render_table(
    headers: list[str],
    rows: list[list[str]],
    pretty: bool,
    row_styles: list[str] | None = None,
    cell_formatters: list[Callable] | None = None,
    highlight_headers: list[int] | None = None,
    highlight_header_styles: dict[int, str] | None = None,
) -> None:
    """Render tabular data either aligned (pretty) or TSV.

    Args:
        headers: Column headers.
        rows: Data rows.
        pretty: If True, align columns; otherwise output TSV.
        row_styles: Optional list of style names per row ("dim", "highlight", or "").
        cell_formatters: Optional list of functions (one per column) to format cell values.
                        Each function takes (value, row_index) and returns formatted string.
        highlight_headers: Optional list of column indices to highlight in the header row.
    """
    if not pretty:
        print("\t".join(headers))
        for row in rows:
            print("\t".join(str(cell) for cell in row))
        return

    widths = [
        max(len(str(headers[idx])), max((len(str(row[idx])) for row in rows), default=0))
        for idx in range(len(headers))
    ]

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
        if style == "other":
            return Color.other(text)
        return text

    def fmt_row(row: list[str], row_idx: int) -> str:
        style = ""
        if row_styles and row_idx < len(row_styles):
            style = row_styles[row_idx]
        parts = []
        for col_idx, cell in enumerate(row):
            cell_str = str(cell).ljust(widths[col_idx])
            has_formatter = (
                cell_formatters
                and col_idx < len(cell_formatters)
                and cell_formatters[col_idx]
            )
            if has_formatter:
                cell_str = cell_formatters[col_idx](cell_str, row_idx)
            elif style:
                cell_str = apply_row_style(cell_str, style)
            parts.append(cell_str)
        return "  ".join(parts)

    def fmt_header(idx: int, h: str) -> str:
        text = str(h).ljust(widths[idx])
        if highlight_header_styles and idx in highlight_header_styles:
            style = highlight_header_styles[idx]
            if style == "highlight":
                return Color.highlight(text)
            if style == "highlight_soft":
                return Color.highlight_soft(text)
        if highlight_headers and idx in highlight_headers:
            return Color.highlight(text)
        return text

    print("  ".join(fmt_header(i, h) for i, h in enumerate(headers)))
    for idx, row in enumerate(rows):
        print(fmt_row(row, idx))


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
