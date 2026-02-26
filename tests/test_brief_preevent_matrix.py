"""Matrix and rendering contract tests for brief preevent."""

import argparse
import datetime

import pytest

from biathlon.cli import build_parser
from biathlon.commands import brief


def test_preevent_section_matrix_has_full_event_coverage():
    expected_sections = set(brief.PREEVENT_SECTION_ORDER)

    assert set(brief.PREEVENT_SECTION_TITLES) == expected_sections
    assert set(brief.PREEVENT_SECTION_MATRIX) == expected_sections

    for section_id in brief.PREEVENT_SECTION_ORDER:
        row = brief.PREEVENT_SECTION_MATRIX[section_id]
        assert set(row) == set(brief.PREEVENT_CATEGORY_CODES)
        for category_code in brief.PREEVENT_CATEGORY_CODES:
            assert isinstance(row[category_code], bool)


def test_preevent_matrix_sample_cells_match_spec():
    assert brief._preevent_section_enabled(brief.PREEVENT_SECTION_EVENT_FACTS, "WC")
    assert brief._preevent_section_enabled(brief.PREEVENT_SECTION_EVENT_AGENDA, "WC")
    assert brief._preevent_section_enabled(
        brief.PREEVENT_SECTION_LAST_10_EDITIONS, "WCH"
    )
    assert brief._preevent_section_enabled(
        brief.PREEVENT_SECTION_ATHLETE_STANDINGS, "WCH"
    )
    assert brief._preevent_section_enabled(
        brief.PREEVENT_SECTION_RELAY_STANDINGS, "OWG"
    )
    assert brief._preevent_section_enabled(brief.PREEVENT_SECTION_NATIONS_CUP, "OWG")
    assert brief._preevent_section_enabled(brief.PREEVENT_SECTION_DECORATED_VENUE, "WC")
    assert brief._preevent_section_enabled(
        brief.PREEVENT_SECTION_DECORATED_EVENT_TYPE, "OWG"
    )


def test_brief_preevent_rejects_removed_flags():
    parser = build_parser()
    with pytest.raises(SystemExit) as men_exc:
        parser.parse_args(["brief", "preevent", "--men"])
    assert men_exc.value.code == 2

    with pytest.raises(SystemExit) as major_exc:
        parser.parse_args(["brief", "preevent", "--major"])
    assert major_exc.value.code == 2


def test_find_current_event_prefers_in_progress(monkeypatch):
    class FakeDate(datetime.date):
        @classmethod
        def today(cls):
            return cls(2026, 1, 10)

    monkeypatch.setattr(brief, "get_current_season_id", lambda: "2526")
    monkeypatch.setattr(
        brief,
        "get_events",
        lambda season_id, level: [
            {
                "EventId": "PAST",
                "StartDate": "2026-01-01",
                "EndDate": "2026-01-05",
            },
            {
                "EventId": "LIVE",
                "StartDate": "2026-01-09",
                "EndDate": "2026-01-12",
            },
            {
                "EventId": "UPCOMING",
                "StartDate": "2026-01-16",
                "EndDate": "2026-01-20",
            },
        ],
    )
    monkeypatch.setattr(brief.datetime, "date", FakeDate)

    event = brief._find_current_event()
    assert event is not None
    assert event.get("EventId") == "LIVE"


def test_resolve_current_season_id_for_highlight_prefers_is_current(monkeypatch):
    monkeypatch.setattr(brief, "get_current_season_id", lambda: "2627")
    monkeypatch.setattr(
        brief,
        "get_seasons",
        lambda: [
            {"SeasonId": "2425", "SortOrder": 1, "IsCurrent": False},
            {"SeasonId": "2526", "SortOrder": 2, "IsCurrent": True},
            {"SeasonId": "2627", "SortOrder": 3, "IsCurrent": False},
        ],
    )

    assert brief._resolve_current_season_id_for_highlight() == "2526"


def test_count_venue_event_editions_groups_wc_wch_owg(monkeypatch):
    monkeypatch.setattr(brief, "get_seasons", lambda: [{"SeasonId": "2526"}])
    monkeypatch.setattr(
        brief,
        "get_events",
        lambda season_id, level: (
            [
                {
                    "EventId": "A",
                    "Organizer": "Kontiolahti",
                    "Description": "BMW IBU World Cup",
                },
                {
                    "EventId": "B",
                    "Organizer": "Kontiolahti",
                    "Description": "BMW IBU World Championships",
                },
                {
                    "EventId": "C",
                    "Organizer": "Kontiolahti",
                    "Description": "Olympic Winter Games",
                },
                {
                    "EventId": "D",
                    "Organizer": "Ostersund",
                    "Description": "BMW IBU World Cup",
                },
                {
                    "EventId": "A",
                    "Organizer": "Kontiolahti",
                    "Description": "BMW IBU World Cup",
                },
            ]
            if season_id == "2526" and level == 1
            else []
        ),
    )

    assert brief._count_venue_event_editions("Kontiolahti") == (1, 1, 1)


def test_count_venue_event_editions_excludes_future_after_reference_date(monkeypatch):
    monkeypatch.setattr(brief, "get_seasons", lambda: [{"SeasonId": "2526"}])
    monkeypatch.setattr(
        brief,
        "get_events",
        lambda season_id, level: (
            [
                {
                    "EventId": "A",
                    "Organizer": "Kontiolahti",
                    "Description": "BMW IBU World Cup",
                    "StartDate": "2026-01-15",
                },
                {
                    "EventId": "B",
                    "Organizer": "Kontiolahti",
                    "Description": "BMW IBU World Championships",
                    "StartDate": "2026-02-10",
                },
            ]
            if season_id == "2526" and level == 1
            else []
        ),
    )

    assert brief._count_venue_event_editions(
        "Kontiolahti", reference_date=datetime.date(2026, 1, 15)
    ) == (1, 0, 0)


def test_recent_venue_editions_rows_include_race_type_counts(monkeypatch):
    monkeypatch.setattr(brief, "get_seasons", lambda: [{"SeasonId": "2526"}])
    monkeypatch.setattr(
        brief,
        "get_events",
        lambda season_id, level: (
            [
                {
                    "EventId": "A",
                    "Organizer": "Kontiolahti",
                    "Description": "BMW IBU World Cup",
                    "StartDate": "2026-01-15",
                }
            ]
            if season_id == "2526" and level == 1
            else []
        ),
    )
    monkeypatch.setattr(
        brief,
        "get_races",
        lambda event_id: (
            [
                {"catId": "SW", "DisciplineId": "SP"},
                {"catId": "SM", "DisciplineId": "SP"},
                {"catId": "SW", "DisciplineId": "SI"},
                {"catId": "SM", "DisciplineId": "PU"},
                {"catId": "MX", "DisciplineId": "RL"},
                {"catId": "SM", "DisciplineId": "SR"},
                {"catId": "JW", "DisciplineId": "SP"},
            ]
            if event_id == "A"
            else []
        ),
    )

    rows = brief._build_recent_venue_edition_rows("Kontiolahti", limit=10)
    assert rows == [["2026-01-15", "WC", 2, 1, 1, 0, 0, 1, 1]]


def test_recent_venue_editions_rows_exclude_future_after_reference_date(monkeypatch):
    monkeypatch.setattr(brief, "get_seasons", lambda: [{"SeasonId": "2526"}])
    monkeypatch.setattr(
        brief,
        "get_events",
        lambda season_id, level: (
            [
                {
                    "EventId": "A",
                    "Organizer": "Kontiolahti",
                    "Description": "BMW IBU World Cup",
                    "StartDate": "2026-01-15",
                },
                {
                    "EventId": "B",
                    "Organizer": "Kontiolahti",
                    "Description": "BMW IBU World Cup",
                    "StartDate": "2026-12-01",
                },
            ]
            if season_id == "2526" and level == 1
            else []
        ),
    )
    monkeypatch.setattr(
        brief,
        "get_races",
        lambda event_id: [{"catId": "SW", "DisciplineId": "SP"}],
    )

    rows = brief._build_recent_venue_edition_rows(
        "Kontiolahti", limit=10, reference_date=datetime.date(2026, 1, 15)
    )
    assert rows == [["2026-01-15", "WC", 1, 0, 0, 0, 0, 0, 0]]


def test_build_venue_decorated_athlete_rows_uses_medal_columns(monkeypatch):
    monkeypatch.setattr(brief, "get_current_season_id", lambda: "2526")
    monkeypatch.setattr(brief, "get_seasons", lambda: [{"SeasonId": "2526"}])
    monkeypatch.setattr(
        brief,
        "get_events",
        lambda season_id, level: (
            [
                {
                    "EventId": "EVT1",
                    "Organizer": "Kontiolahti",
                    "Description": "BMW IBU World Cup",
                    "StartDate": "2026-01-10",
                }
            ]
            if season_id == "2526" and level == 1
            else []
        ),
    )
    monkeypatch.setattr(
        brief,
        "get_races",
        lambda event_id: (
            [
                {"RaceId": "R_SW", "catId": "SW", "DisciplineId": "SP"},
                {"RaceId": "R_MX", "catId": "MX", "DisciplineId": "SR"},
                {"RaceId": "R_SM", "catId": "SM", "DisciplineId": "SP"},
            ]
            if event_id == "EVT1"
            else []
        ),
    )

    payloads = {
        "R_SW": {
            "Results": [
                {
                    "IsTeam": False,
                    "IBUId": "W1",
                    "Name": "Alice",
                    "Nat": "NOR",
                    "Rank": "1",
                }
            ]
        },
        "R_MX": {
            "Results": [
                {"IsTeam": True, "Bib": "7", "Nat": "FRA", "Rank": "1"},
                {
                    "IsTeam": False,
                    "IBUId": "F1",
                    "Name": "Fiona",
                    "Nat": "FRA",
                    "Bib": "7",
                    "Leg": "1",
                },
                {
                    "IsTeam": False,
                    "IBUId": "M1",
                    "Name": "Marc",
                    "Nat": "FRA",
                    "Bib": "7",
                    "Leg": "2",
                },
            ]
        },
        "R_SM": {
            "Results": [
                {
                    "IsTeam": False,
                    "IBUId": "M1",
                    "Name": "Marc",
                    "Nat": "FRA",
                    "Rank": "2",
                }
            ]
        },
    }
    monkeypatch.setattr(brief, "get_race_results", lambda race_id: payloads[race_id])

    rows, row_styles = brief._build_venue_decorated_athlete_rows(
        "Kontiolahti", limit=10
    )

    assert rows[0][1] == "Alice"
    assert any(
        row[1] == "Marc"
        and row[2] == "FRA"
        and row[3] == "M"
        and row[4:9] == ["1", "1", "0", "2", "2"]
        and row[9:14] == ["0", "1", "0", "1", "1"]
        and row[14:19] == ["1", "0", "0", "1", "1"]
        for row in rows
    )
    assert any(
        row[1] == "Alice"
        and row[4:9] == ["1", "0", "0", "1", "1"]
        and row[9:14] == ["1", "0", "0", "1", "1"]
        and row[14:19] == ["0", "0", "0", "0", "0"]
        for row in rows
    )
    assert any(
        row[1] == "Fiona" and row[3] == "F" and row[14:19] == ["1", "0", "0", "1", "1"]
        for row in rows
    )
    assert row_styles
    assert all(style == "highlight_plain" for style in row_styles)


def test_build_venue_decorated_athlete_rows_respects_global_highlight_keys(monkeypatch):
    monkeypatch.setattr(brief, "get_current_season_id", lambda: "2526")
    monkeypatch.setattr(
        brief,
        "get_seasons",
        lambda: [
            {"SeasonId": "2526", "SortOrder": 2},
            {"SeasonId": "2425", "SortOrder": 1},
        ],
    )
    monkeypatch.setattr(
        brief,
        "get_events",
        lambda season_id, level: (
            [
                {
                    "EventId": "OLD_EVT",
                    "Organizer": "Kontiolahti",
                    "Description": "BMW IBU World Cup",
                    "StartDate": "2025-01-10",
                }
            ]
            if season_id == "2425" and level == 1
            else []
        ),
    )
    monkeypatch.setattr(
        brief,
        "get_races",
        lambda event_id: (
            [{"RaceId": "OLD_RACE", "catId": "SM", "DisciplineId": "SP"}]
            if event_id == "OLD_EVT"
            else []
        ),
    )
    monkeypatch.setattr(
        brief,
        "get_race_results",
        lambda race_id: (
            {
                "Results": [
                    {
                        "IsTeam": False,
                        "IBUId": "OLD_ATH",
                        "Name": "Old Athlete",
                        "Nat": "NOR",
                        "Rank": "1",
                    }
                ]
            }
            if race_id == "OLD_RACE"
            else {"Results": []}
        ),
    )

    rows, row_styles = brief._build_venue_decorated_athlete_rows(
        "Kontiolahti",
        highlight_keys={"OLD_ATH"},
        limit=10,
    )

    assert rows
    assert row_styles == ["highlight_plain"]


def test_render_decorated_tables_renumbers_rank_per_gender(capsys):
    rows = [
        [
            "5",
            "Woman A",
            "SWE",
            "F",
            "2",
            "1",
            "0",
            "3",
            "10",
            "1",
            "1",
            "0",
            "2",
            "8",
            "1",
            "0",
            "0",
            "1",
            "2",
        ],
        [
            "9",
            "Man A",
            "NOR",
            "M",
            "4",
            "2",
            "1",
            "7",
            "14",
            "3",
            "1",
            "1",
            "5",
            "11",
            "1",
            "1",
            "0",
            "2",
            "3",
        ],
    ]
    styles = ["", ""]

    brief._render_decorated_athletes_split_tables(
        "Most Decorated Athletes at Kontiolahti",
        rows,
        styles,
        argparse.Namespace(format="tsv"),
    )

    out = capsys.readouterr().out
    assert "### Women" in out
    assert "### Men" in out
    assert "1\tWoman A\tSWE" in out
    assert "1\tMan A\tNOR" in out
    assert "5\tWoman A\tSWE" not in out
    assert "9\tMan A\tNOR" not in out


def test_render_decorated_tables_respects_per_gender_limit(capsys):
    rows = [
        [
            "1",
            "Woman A",
            "SWE",
            "F",
            "3",
            "0",
            "0",
            "3",
            "8",
            "3",
            "0",
            "0",
            "3",
            "8",
            "0",
            "0",
            "0",
            "0",
            "0",
        ],
        [
            "2",
            "Man A",
            "NOR",
            "M",
            "3",
            "0",
            "0",
            "3",
            "8",
            "3",
            "0",
            "0",
            "3",
            "8",
            "0",
            "0",
            "0",
            "0",
            "0",
        ],
        [
            "3",
            "Woman B",
            "GER",
            "F",
            "2",
            "1",
            "0",
            "3",
            "9",
            "2",
            "1",
            "0",
            "3",
            "9",
            "0",
            "0",
            "0",
            "0",
            "0",
        ],
        [
            "4",
            "Man B",
            "FRA",
            "M",
            "2",
            "1",
            "0",
            "3",
            "9",
            "2",
            "1",
            "0",
            "3",
            "9",
            "0",
            "0",
            "0",
            "0",
            "0",
        ],
        [
            "5",
            "Woman C",
            "ITA",
            "F",
            "1",
            "1",
            "1",
            "3",
            "10",
            "1",
            "1",
            "1",
            "3",
            "10",
            "0",
            "0",
            "0",
            "0",
            "0",
        ],
    ]
    styles = ["", "", "", "", ""]

    brief._render_decorated_athletes_split_tables(
        "Most Decorated Athletes at Kontiolahti",
        rows,
        styles,
        argparse.Namespace(format="tsv"),
        per_gender_limit=2,
    )

    out = capsys.readouterr().out
    assert "Woman A" in out
    assert "Woman B" in out
    assert "Woman C" not in out
    assert "Man A" in out
    assert "Man B" in out


def test_handle_brief_preevent_renders_matrix_sections(monkeypatch, capsys):
    monkeypatch.setattr(brief, "get_current_season_id", lambda: "2526")
    monkeypatch.setattr(
        brief,
        "_find_current_event",
        lambda: {
            "EventId": "EVT1",
            "SeasonId": "2526",
            "Organizer": "Ruhpolding",
            "Nat": "GER",
            "Description": "BMW IBU World Cup",
        },
    )
    monkeypatch.setattr(
        brief,
        "get_seasons",
        lambda: [{"SeasonId": "2526"}, {"SeasonId": "2425"}],
    )
    monkeypatch.setattr(
        brief,
        "get_events",
        lambda season_id, level: (
            [
                {
                    "EventId": "EVT0",
                    "Organizer": "Ruhpolding",
                    "Description": "BMW IBU World Cup",
                    "StartDate": "2025-12-30",
                    "EndDate": "2026-01-03",
                },
                {
                    "EventId": "EVT1",
                    "Organizer": "Ruhpolding",
                    "Description": "BMW IBU World Cup",
                    "StartDate": "2026-01-10",
                    "EndDate": "2026-01-16",
                },
                {
                    "EventId": "EVT_WCH",
                    "Organizer": "Ruhpolding",
                    "Description": "BMW IBU World Championships",
                    "StartDate": "2026-02-01",
                    "EndDate": "2026-02-14",
                },
            ]
            if season_id == "2526" and level == 1
            else [
                {
                    "EventId": "EVT_OWG",
                    "Organizer": "Ruhpolding",
                    "Description": "Olympic Winter Games",
                    "StartDate": "2025-02-01",
                    "EndDate": "2025-02-14",
                },
                {
                    "EventId": "EVT_OTHER",
                    "Organizer": "Ostersund",
                    "Description": "BMW IBU World Cup",
                    "StartDate": "2025-03-01",
                    "EndDate": "2025-03-05",
                },
            ]
            if season_id == "2425" and level == 1
            else []
        ),
    )

    def fake_get_races(event_id: str):
        if event_id == "EVT1":
            return [
                {
                    "RaceId": "TARGET",
                    "catId": "SW",
                    "DisciplineId": "SP",
                    "StartTime": "2026-01-10T10:00:00Z",
                },
                {
                    "RaceId": "TARGET2",
                    "catId": "SM",
                    "DisciplineId": "SP",
                    "StartTime": "2026-01-10T12:00:00Z",
                },
            ]
        if event_id == "EVT0":
            return [
                {
                    "RaceId": "R_SW_SP",
                    "catId": "SW",
                    "DisciplineId": "SP",
                    "StartTime": "2026-01-01T10:00:00Z",
                },
                {
                    "RaceId": "R_SM_SP",
                    "catId": "SM",
                    "DisciplineId": "SP",
                    "StartTime": "2026-01-01T12:00:00Z",
                },
                {
                    "RaceId": "R_SW_RL",
                    "catId": "SW",
                    "DisciplineId": "RL",
                    "StartTime": "2026-01-02T10:00:00Z",
                },
                {
                    "RaceId": "R_SM_RL",
                    "catId": "SM",
                    "DisciplineId": "RL",
                    "StartTime": "2026-01-02T12:00:00Z",
                },
                {
                    "RaceId": "R_MX_MR",
                    "catId": "MX",
                    "DisciplineId": "MR",
                    "StartTime": "2026-01-03T10:00:00Z",
                },
            ]
        return []

    monkeypatch.setattr(brief, "get_races", fake_get_races)

    def fake_get_race_results(race_id: str):
        if race_id == "TARGET":
            return {
                "SportEvt": {
                    "Description": "BMW IBU World Cup",
                    "Organizer": "Ruhpolding",
                },
                "Competition": {
                    "DisciplineId": "SP",
                    "catId": "SW",
                    "StartTime": "2026-01-10T10:00:00Z",
                },
                "Results": [],
            }
        payloads = {
            "TARGET2": {"Results": []},
            "R_SW_SP": {
                "Results": [
                    {
                        "IsTeam": False,
                        "IBUId": "W1",
                        "Name": "Woman One",
                        "Nat": "NOR",
                        "Rank": "1",
                        "Result": "20:00.0",
                    }
                ]
            },
            "R_SM_SP": {
                "Results": [
                    {
                        "IsTeam": False,
                        "IBUId": "M1",
                        "Name": "Man One",
                        "Nat": "FRA",
                        "Rank": "1",
                        "Result": "21:00.0",
                    }
                ]
            },
            "R_SW_RL": {
                "Results": [
                    {
                        "IsTeam": True,
                        "Name": "Norway",
                        "Nat": "NOR",
                        "Rank": "1",
                        "Result": "1:10:00.0",
                    }
                ]
            },
            "R_SM_RL": {
                "Results": [
                    {
                        "IsTeam": True,
                        "Name": "France",
                        "Nat": "FRA",
                        "Rank": "1",
                        "Result": "1:11:00.0",
                    }
                ]
            },
            "R_MX_MR": {
                "Results": [
                    {
                        "IsTeam": True,
                        "Name": "Sweden",
                        "Nat": "SWE",
                        "Rank": "1",
                        "Result": "40:00.0",
                    }
                ]
            },
        }
        return payloads.get(race_id, {"Results": []})

    monkeypatch.setattr(brief, "get_race_results", fake_get_race_results)

    args = argparse.Namespace(event="", format="tsv")
    rc = brief.handle_brief_preevent(args)

    assert rc == 0
    out = capsys.readouterr().out
    assert "# Event Brief - Ruhpolding" in out
    assert "## Event Facts" in out
    assert "Country\tWC Editions\tWCH Editions\tOWG Editions" in out
    assert "Germany\t2\t0\t1" in out
    assert "## Last 10 Editions at Ruhpolding" in out
    assert out.index("## Event Agenda") < out.index("## Last 10 Editions at Ruhpolding")
    assert (
        "Edition\tType\tSprint\tPursuit\tIndividual\tMass Start\tRelay\tMixed Relay\tSingle Mixed Relay"
        in out
    )
    assert "2026-01-10\tWC\tX\t-\t-\t-\t-\t-\t-" in out
    assert "2026-02-01\tWCH" not in out
    assert "## Event Agenda" in out
    assert "## Athlete Standings" in out
    assert "## Relay Standings" in out
    assert "## Nations Cup Standings" in out
    assert "## Most Decorated Athletes at Ruhpolding" in out
    assert "## Most Decorated Athletes at World Cup" in out
    assert out.rfind("## Most Decorated Athletes at World Cup") > out.rfind(
        "## Nations Cup Standings"
    )
    assert (
        "#\tAthlete\tNat\tGold\tSilver\tBronze\tTotal\tRaces\tGold\tSilver\tBronze\tTotal\tRaces\tGold\tSilver\tBronze\tTotal\tRaces"
        in out
    )
    assert "### Women" in out
    assert "### Men" in out
    assert "Date\tDay\tTime\tCategory\tDiscipline\tSeason Race\tSeason Race Full" in out
    assert out.count("\t2/2\t2/2\n") >= 2
    assert (
        "Position\tName\tCountry\tAge\tTotal\tSprint\tPursuit\tIndividual\tMassStart"
        in out
    )
    assert "Overall - Women" not in out
    assert "Overall - Men" not in out
    assert "Mixed Relay" in out
    assert "Women" in out
    assert "Men" in out


def test_sequence_maps_use_event_level_and_include_mixed_in_team_full(monkeypatch):
    monkeypatch.setattr(
        brief,
        "get_events",
        lambda season_id, level: (
            [{"EventId": "EVT_L1", "Description": "BMW IBU World Cup"}]
            if level == 1
            else [{"EventId": "EVT_LX", "Description": "BMW IBU World Cup"}]
        ),
    )

    def fake_get_races(event_id: str):
        if event_id == "EVT_L1":
            return [
                {
                    "RaceId": "SW_IN_1",
                    "catId": "SW",
                    "DisciplineId": "IN",
                    "StartTime": "2026-01-01T10:00:00Z",
                },
                {
                    "RaceId": "SW_IN_2",
                    "catId": "SW",
                    "DisciplineId": "IN",
                    "StartTime": "2026-01-02T10:00:00Z",
                },
                {
                    "RaceId": "SW_RL_1",
                    "catId": "SW",
                    "DisciplineId": "RL",
                    "StartTime": "2026-01-03T10:00:00Z",
                },
                {
                    "RaceId": "MX_MR_1",
                    "catId": "MX",
                    "DisciplineId": "MR",
                    "StartTime": "2026-01-04T10:00:00Z",
                },
            ]
        if event_id == "EVT_LX":
            return [
                {
                    "RaceId": "SW_IN_X",
                    "catId": "SW",
                    "DisciplineId": "IN",
                    "StartTime": "2026-01-05T10:00:00Z",
                }
            ]
        return []

    monkeypatch.setattr(brief, "get_races", fake_get_races)

    disc_map, full_map = brief._build_season_race_sequence_maps(
        "2526", brief.EVENT_TYPE_WC, level=1
    )

    # Level-2 races are excluded from level-1 sequence totals.
    assert disc_map[("SW", "SW_IN_2")] == "2/2"
    # Full individual index tracks all SW individual races.
    assert full_map[("SW", "SW_IN_2")] == "2/2"
    # Full team index for SW includes mixed relay races.
    assert full_map[("SW", "SW_RL_1")] == "1/2"
    assert full_map[("SW", "MX_MR_1")] == "2/2"


def test_render_preevent_agenda_non_wc_hides_season_race_columns(monkeypatch, capsys):
    monkeypatch.setattr(
        brief,
        "_build_season_race_sequence_maps",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not be called")),
    )

    brief._render_preevent_agenda(
        [
            {
                "RaceId": "R1",
                "catId": "SW",
                "DisciplineId": "IN",
                "StartTime": "2026-02-10T13:05:00Z",
            }
        ],
        argparse.Namespace(format="tsv"),
        season_id="2526",
        event_type=brief.EVENT_TYPE_WCH,
        event_id="EVT_WCH",
        level=1,
    )

    out = capsys.readouterr().out
    assert "Date\tDay\tTime\tCategory\tDiscipline\n" in out
    assert "Season Race" not in out
    assert "Season Race Full" not in out


def test_snapshot_athlete_standings_pretty_adds_leader_markers(monkeypatch, capsys):
    monkeypatch.setattr(brief.Color, "enabled", classmethod(lambda cls: True))
    monkeypatch.setattr(
        brief,
        "_prefetch_bios",
        lambda ibu_ids: {
            "A": {"BirthDate": "1998-01-01"},
            "B": {"BirthDate": "1999-01-01"},
        },
    )

    total_rows = [
        {"Rank": 1, "IBUId": "A", "Name": "Leader", "Nat": "NOR", "Score": 130},
        {"Rank": 2, "IBUId": "B", "Name": "Chaser", "Nat": "FRA", "Score": 120},
    ]
    discipline_rows = {
        "SP": [{"Rank": 1, "IBUId": "A", "Name": "Leader", "Nat": "NOR", "Score": 70}],
        "PU": [{"Rank": 1, "IBUId": "B", "Name": "Chaser", "Nat": "FRA", "Score": 60}],
        "IN": [],
        "MS": [],
    }

    brief._render_snapshot_athlete_standings_table(
        "Women",
        total_rows,
        discipline_rows,
        argparse.Namespace(format=""),
        reference_date=datetime.date(2026, 1, 10),
    )

    out = capsys.readouterr().out
    assert "●" in out
    assert "\x1b[" in out
    assert "\x1b[38;2;218;165;32m60" in out


def test_snapshot_athlete_standings_fills_age(monkeypatch, capsys):
    monkeypatch.setattr(
        brief,
        "_prefetch_bios",
        lambda ibu_ids: {
            "A": {"BirthDate": "2000-01-10"},
            "B": {"Personal": [{"Description": "Age", "Value": "24"}]},
        },
    )

    total_rows = [
        {"Rank": 1, "IBUId": "A", "Name": "Leader", "Nat": "NOR", "Score": 130},
        {"Rank": 2, "IBUId": "B", "Name": "Chaser", "Nat": "FRA", "Score": 120},
    ]
    discipline_rows = {"SP": [], "PU": [], "IN": [], "MS": []}

    brief._render_snapshot_athlete_standings_table(
        "Women",
        total_rows,
        discipline_rows,
        argparse.Namespace(format="tsv"),
        reference_date=datetime.date(2026, 1, 10),
    )

    out = capsys.readouterr().out
    assert "\t26\t130\t" in out
    assert "\t24\t120\t" in out


def test_snapshot_athlete_standings_keeps_non_top10_discipline_points(
    monkeypatch, capsys
):
    monkeypatch.setattr(
        brief,
        "get_events",
        lambda season_id, level: [
            {"EventId": "EVT0", "Description": "BMW IBU World Cup"}
        ],
    )
    monkeypatch.setattr(
        brief,
        "get_races",
        lambda event_id: [
            {
                "RaceId": "R_SP",
                "catId": "SW",
                "DisciplineId": "SP",
                "StartTime": "2026-01-01T10:00:00Z",
            },
            {
                "RaceId": "R_PU",
                "catId": "SW",
                "DisciplineId": "PU",
                "StartTime": "2026-01-02T10:00:00Z",
            },
        ],
    )

    athletes = [
        ("A", "Athlete A"),
        ("B", "Athlete B"),
        ("C", "Athlete C"),
        ("D", "Athlete D"),
        ("E", "Athlete E"),
        ("F", "Athlete F"),
        ("G", "Athlete G"),
        ("H", "Athlete H"),
        ("I", "Athlete I"),
        ("J", "Athlete J"),
        ("K", "Braisaz Bouchet"),
    ]

    sp_results = []
    for rank, (ibu_id, name) in enumerate(athletes, start=1):
        sp_results.append(
            {
                "IsTeam": False,
                "IBUId": ibu_id,
                "Name": name,
                "Nat": "FRA",
                "Rank": str(rank),
                "Result": "20:00.0",
            }
        )

    pu_results = [
        {
            "IsTeam": False,
            "IBUId": "K",
            "Name": "Braisaz Bouchet",
            "Nat": "FRA",
            "Rank": "1",
            "Result": "21:00.0",
        }
    ]

    monkeypatch.setattr(
        brief,
        "get_race_results",
        lambda race_id: {"Results": sp_results if race_id == "R_SP" else pu_results},
    )
    monkeypatch.setattr(brief, "_prefetch_bios", lambda ibu_ids: {})

    cutoff = datetime.datetime(2026, 1, 10, tzinfo=datetime.timezone.utc)
    standings = brief._compute_preevent_snapshot_standings(
        "2526", "TARGET", cutoff, limit=10
    )
    women_rows = standings["athlete"]["SW"]
    expected_sp = brief._get_wc_points(11)
    expected_pu = brief._get_wc_points(1)

    brief._render_snapshot_athlete_standings_table(
        "Women",
        women_rows["TS"],
        women_rows,
        argparse.Namespace(format="tsv"),
        reference_date=cutoff.date(),
    )

    out = capsys.readouterr().out
    assert "Braisaz Bouchet" in out
    assert f"\t{expected_sp}\t{expected_pu}\t" in out


def test_render_relay_tables_pretty_side_by_side(monkeypatch, capsys):
    monkeypatch.setattr(brief.Color, "enabled", classmethod(lambda cls: False))

    brief._render_relay_tables(
        {
            "SW": [{"Rank": 1, "Name": "Norway", "Nat": "NOR", "Score": 100}],
            "SM": [{"Rank": 1, "Name": "France", "Nat": "FRA", "Score": 98}],
            "MX": [{"Rank": 1, "Name": "Sweden", "Nat": "SWE", "Score": 95}],
        },
        argparse.Namespace(format=""),
    )

    out = capsys.readouterr().out
    assert "Women Relay" in out
    assert "Men Relay" in out
    assert "Mixed Relay" in out
    assert "All Relay (unofficial)" in out
    assert out.count("Rank") >= 3
    assert "Norway" in out and "France" in out and "Sweden" in out
    assert "Nat" not in out


def test_render_relay_tables_tsv_includes_all_relay_sum(capsys):
    brief._render_relay_tables(
        {
            "SW": [
                {"Rank": 1, "Name": "Norway", "Nat": "NOR", "Score": 100},
                {"Rank": 2, "Name": "France", "Nat": "FRA", "Score": 80},
            ],
            "SM": [{"Rank": 1, "Name": "Norway", "Nat": "NOR", "Score": 90}],
            "MX": [{"Rank": 1, "Name": "Norway", "Nat": "NOR", "Score": 75}],
        },
        argparse.Namespace(format="tsv"),
    )

    out = capsys.readouterr().out
    assert "All Relay (unofficial)" in out
    assert "1\tNorway\t265" in out
    assert "2\tFrance\t80" in out


def test_render_nations_tables_pretty_side_by_side(monkeypatch, capsys):
    monkeypatch.setattr(brief.Color, "enabled", classmethod(lambda cls: False))

    brief._render_nations_tables(
        {
            "SW": [
                {"Rank": 1, "Name": "FRANCE", "Nat": "FRA", "Score": 1000},
                {"Rank": 2, "Name": "NORWAY", "Nat": "NOR", "Score": 700},
            ],
            "SM": [
                {"Rank": 1, "Name": "FRANCE", "Nat": "FRA", "Score": 995},
                {"Rank": 2, "Name": "NORWAY", "Nat": "NOR", "Score": 200},
            ],
        },
        argparse.Namespace(format=""),
    )

    out = capsys.readouterr().out
    assert "Women" in out
    assert "Men" in out
    assert "Combined (unofficial)" in out
    assert "France" in out and "Norway" in out
    assert "FRANCE" not in out and "NORWAY" not in out
    assert out.count("Team") >= 2
    assert "1000.0" in out
    assert "995.0" in out
    assert "1995.0" not in out
    assert "1995" in out


def test_render_nations_tables_tsv_includes_all_nations_sum(capsys):
    brief._render_nations_tables(
        {
            "SW": [
                {"Rank": 1, "Name": "FRANCE", "Nat": "FRA", "Score": 1000},
                {"Rank": 2, "Name": "NORWAY", "Nat": "NOR", "Score": 700.5},
            ],
            "SM": [
                {"Rank": 1, "Name": "FRANCE", "Nat": "FRA", "Score": 995},
                {"Rank": 2, "Name": "NORWAY", "Nat": "NOR", "Score": 200},
            ],
        },
        argparse.Namespace(format="tsv"),
    )

    out = capsys.readouterr().out
    assert "Combined (unofficial)" in out
    assert "1\tFrance\tFRA\t1995.0" in out
    assert "2\tNorway\tNOR\t900.5" in out


def test_relay_and_nations_use_same_country_mapping(monkeypatch, capsys):
    monkeypatch.setattr(brief.Color, "enabled", classmethod(lambda cls: False))

    brief._render_relay_tables(
        {"SW": [{"Rank": 1, "Name": "CZECHIA", "Nat": "CZE", "Score": 100}]},
        argparse.Namespace(format=""),
    )
    relay_out = capsys.readouterr().out

    brief._render_nations_tables(
        {"SW": [{"Rank": 1, "Name": "CZECHIA", "Nat": "CZE", "Score": 200}]},
        argparse.Namespace(format=""),
    )
    nations_out = capsys.readouterr().out

    assert "Czech Republic" in relay_out
    assert "Czech Republic" in nations_out
    assert "CZECHIA" not in relay_out
    assert "CZECHIA" not in nations_out


def test_handle_brief_preevent_upcoming_uses_live_cup_athlete_rows(monkeypatch, capsys):
    monkeypatch.setattr(brief, "get_current_season_id", lambda: "2526")
    monkeypatch.setattr(
        brief,
        "_find_current_event",
        lambda: {
            "EventId": "EVT1",
            "SeasonId": "2526",
            "Organizer": "Ruhpolding",
            "Description": "BMW IBU World Cup",
        },
    )
    monkeypatch.setattr(brief, "get_seasons", lambda: [])
    monkeypatch.setattr(brief, "get_events", lambda season_id, level: [])
    monkeypatch.setattr(
        brief,
        "get_races",
        lambda event_id: [
            {
                "RaceId": "TARGET",
                "catId": "SW",
                "DisciplineId": "SP",
                "StartTime": "2099-01-10T10:00:00Z",
            }
        ]
        if event_id == "EVT1"
        else [],
    )
    monkeypatch.setattr(
        brief,
        "get_race_results",
        lambda race_id: {
            "SportEvt": {"Description": "BMW IBU World Cup", "Organizer": "Ruhpolding"},
            "Competition": {
                "DisciplineId": "SP",
                "catId": "SW",
                "StartTime": "2099-01-10T10:00:00Z",
            },
            "Results": [],
        },
    )
    monkeypatch.setattr(
        brief,
        "_compute_preevent_snapshot_standings",
        lambda *a, **k: {
            "athlete": {
                "SW": {"TS": [], "SP": [], "PU": [], "IN": [], "MS": []},
                "SM": {"TS": [], "SP": [], "PU": [], "IN": [], "MS": []},
            },
            "relay": {"SW": [], "SM": [], "MX": []},
            "nations": {"SW": [], "SM": []},
        },
    )
    monkeypatch.setattr(
        brief,
        "_fetch_live_athlete_cup_rows",
        lambda season_id: {
            "SW": {
                "TS": [
                    {
                        "Rank": 1,
                        "IBUId": "A",
                        "Name": "Cup Leader",
                        "Nat": "NOR",
                        "Score": 300,
                    }
                ],
                "SP": [
                    {
                        "Rank": 1,
                        "IBUId": "A",
                        "Name": "Cup Leader",
                        "Nat": "NOR",
                        "Score": 120,
                    }
                ],
                "PU": [
                    {
                        "Rank": 1,
                        "IBUId": "A",
                        "Name": "Cup Leader",
                        "Nat": "NOR",
                        "Score": 100,
                    }
                ],
                "IN": [
                    {
                        "Rank": 1,
                        "IBUId": "A",
                        "Name": "Cup Leader",
                        "Nat": "NOR",
                        "Score": 80,
                    }
                ],
                "MS": [],
            },
            "SM": {"TS": [], "SP": [], "PU": [], "IN": [], "MS": []},
        },
    )
    monkeypatch.setattr(brief, "_prefetch_bios", lambda ibu_ids: {})

    args = argparse.Namespace(event="", format="tsv")
    rc = brief.handle_brief_preevent(args)

    assert rc == 0
    out = capsys.readouterr().out
    assert "Cup Leader" in out
    assert "\t120\t100\t80\t-\n" in out
