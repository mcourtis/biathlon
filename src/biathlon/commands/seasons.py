"""Seasons command handler."""

from __future__ import annotations

import argparse

from ..api import fetch_json
from ..formatting import is_pretty_output, get_output_format, render_table


def compute_season_styles(seasons: list[dict]) -> list[str]:
    """Return style names for each season row: dim for past, highlight for current."""
    styles: list[str] = []
    current_sort_order: int | None = None
    for season in seasons:
        if season.get("IsCurrent"):
            current_sort_order = season.get("SortOrder", 0)
            break

    for season in seasons:
        sort_order = season.get("SortOrder", 0)
        if season.get("IsCurrent"):
            styles.append("highlight")
        elif current_sort_order is not None and sort_order < current_sort_order:
            styles.append("dim")
        else:
            styles.append("")
    return styles


def handle_seasons(args: argparse.Namespace) -> int:
    """List all seasons available on the API."""
    seasons = fetch_json("Seasons")
    sorted_seasons = sorted(
        seasons,
        key=lambda season: season.get("SortOrder", 0),
        reverse=True,
    )

    # Apply display limit
    limit_n = getattr(args, "limit", 25) or 0
    if limit_n > 0:
        sorted_seasons = sorted_seasons[:limit_n]

    pretty = is_pretty_output(args)
    output_format = get_output_format(args)
    rows: list[list[str]] = []
    for season in sorted_seasons:
        season_id = season.get("SeasonId", "")
        desc = season.get("Description", "")
        if season.get("IsCurrent"):
            desc = f"{desc} *"
        rows.append([season_id, desc])

    row_styles = compute_season_styles(sorted_seasons) if pretty else None
    render_table(
        ["Season", "Description"],
        rows,
        output_format=output_format,
        row_styles=row_styles,
    )
    return 0
