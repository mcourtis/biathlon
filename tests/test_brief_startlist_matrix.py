"""Matrix contract tests for brief startlist section visibility."""

import argparse

import pytest

from biathlon.cli import build_parser
from biathlon.commands import startlist


def test_startlist_section_matrix_has_full_cartesian_coverage():
    expected_sections = set(startlist.STARTLIST_SECTION_ORDER)

    assert set(startlist.STARTLIST_SECTION_TITLES) == expected_sections
    assert set(startlist.STARTLIST_SECTION_MATRIX) == expected_sections

    for section_id in startlist.STARTLIST_SECTION_ORDER:
        row = startlist.STARTLIST_SECTION_MATRIX[section_id]
        assert set(row) == set(startlist.STARTLIST_CATEGORY_CODES)
        for category_code in startlist.STARTLIST_CATEGORY_CODES:
            col = row[category_code]
            assert set(col) == set(startlist.STARTLIST_DISCIPLINE_CODES)
            for discipline_code in startlist.STARTLIST_DISCIPLINE_CODES:
                assert isinstance(col[discipline_code], bool)


def test_startlist_matrix_sample_cells_match_spec():
    assert startlist._section_enabled(
        startlist.SECTION_STANDINGS_WATCH,
        "WC",
        "SR",
    )
    assert not startlist._section_enabled(
        startlist.SECTION_STANDINGS_WATCH,
        "WCH",
        "SR",
    )
    assert startlist._section_enabled(
        startlist.SECTION_PARTICIPATING_TEAMS,
        "OWG",
        "SR",
    )
    assert not startlist._section_enabled(
        startlist.SECTION_NATIONS_CUP,
        "OWG",
        "SI",
    )


def test_brief_startlist_rejects_major_flag():
    parser = build_parser()
    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["brief", "startlist", "--major"])
    assert exc_info.value.code == 2


def test_none_marker_is_lowercase(capsys):
    args = argparse.Namespace(format="tsv")

    startlist._print_section_none(startlist.SECTION_RELAY_WC, args)

    assert "Relay WC Standings (Top 10): none" in capsys.readouterr().out
