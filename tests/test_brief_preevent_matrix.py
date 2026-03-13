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
        brief.PREEVENT_SECTION_PREVIOUS_WINNERS, "WCH"
    )
    assert brief._preevent_section_enabled(
        brief.PREEVENT_SECTION_PREVIOUS_PODIUM, "WCH"
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
    assert brief._preevent_section_enabled(
        brief.PREEVENT_SECTION_DECORATED_MAJOR_EVENTS, "WC"
    )
    assert brief._preevent_section_enabled(
        brief.PREEVENT_SECTION_DECORATED_MAJOR_EVENTS, "WCH"
    )
    assert brief._preevent_section_enabled(
        brief.PREEVENT_SECTION_DECORATED_MAJOR_EVENTS, "OWG"
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


def test_recent_edition_row_styles_highlight_selected_event():
    selected_entries = [
        {"event_id": "E2", "start_date": "2026-01-15"},
        {"event_id": "E1", "start_date": "2025-01-15"},
    ]

    styles = brief._recent_edition_row_styles(
        selected_entries, highlight_event_ids={"E1"}
    )

    assert styles == ["", "highlight"]


def test_previous_venue_podium_rows_follow_upcoming_races_and_limit(monkeypatch):
    upcoming_races = [
        {
            "RaceId": "CUR_SW_IN",
            "catId": "SW",
            "DisciplineId": "IN",
            "StartTime": "2026-01-10T10:00:00Z",
        },
        {
            "RaceId": "CUR_SM_IN",
            "catId": "SM",
            "DisciplineId": "IN",
            "StartTime": "2026-01-10T12:00:00Z",
        },
        {
            "RaceId": "CUR_SW_RL",
            "catId": "SW",
            "DisciplineId": "RL",
            "StartTime": "2026-01-11T10:00:00Z",
        },
    ]
    venue_events = [
        {
            "event_id": "EVT_CUR",
            "season_id": "2526",
            "start_date": "2026-01-10",
            "event": {"Description": "BMW IBU World Cup"},
        },
        {
            "event_id": "EVT_2025",
            "season_id": "2425",
            "start_date": "2025-01-10",
            "event": {"Description": "BMW IBU World Cup"},
        },
        {
            "event_id": "EVT_2024",
            "season_id": "2324",
            "start_date": "2024-01-10",
            "event": {"Description": "BMW IBU World Cup"},
        },
        {
            "event_id": "EVT_2023",
            "season_id": "2223",
            "start_date": "2023-01-10",
            "event": {"Description": "BMW IBU World Cup"},
        },
        {
            "event_id": "EVT_2022",
            "season_id": "2122",
            "start_date": "2022-01-10",
            "event": {"Description": "BMW IBU World Cup"},
        },
        {
            "event_id": "EVT_2021",
            "season_id": "2021",
            "start_date": "2021-01-10",
            "event": {"Description": "BMW IBU World Cup"},
        },
        {
            "event_id": "EVT_2020",
            "season_id": "1920",
            "start_date": "2020-01-10",
            "event": {"Description": "BMW IBU World Cup"},
        },
    ]

    def fake_get_races(event_id: str):
        if not event_id.startswith("EVT_20"):
            return []
        year = event_id.split("_")[1]
        rows = [
            {
                "RaceId": f"{event_id}_SW_IN",
                "catId": "SW",
                "DisciplineId": "IN",
                "StartTime": f"{year}-01-10T10:00:00Z",
            },
            {
                "RaceId": f"{event_id}_SM_IN",
                "catId": "SM",
                "DisciplineId": "IN",
                "StartTime": f"{year}-01-10T12:00:00Z",
            },
        ]
        if event_id != "EVT_2022":
            rows.append(
                {
                    "RaceId": f"{event_id}_SW_RL",
                    "catId": "SW",
                    "DisciplineId": "RL",
                    "StartTime": f"{year}-01-11T10:00:00Z",
                }
            )
        return rows

    monkeypatch.setattr(brief, "get_races", fake_get_races)

    relay_medals = {
        "2025": ("NOR", "FRA", "SWE"),
        "2024": ("FRA", "NOR", "GER"),
        "2023": ("SWE", "GER", "ITA"),
        "2021": ("GER", "SWE", "NOR"),
    }

    def fake_get_race_results(race_id: str):
        parts = race_id.split("_")
        year = parts[1] if len(parts) >= 4 else "0000"
        cat = parts[2] if len(parts) >= 4 else ""
        disc = parts[3] if len(parts) >= 4 else ""
        if disc == "IN":
            if cat == "SW":
                names = (
                    f"Alice WGold{year}",
                    f"Bianca WSilver{year}",
                    f"Chloe WBronze{year}",
                )
                nat = "NOR"
            else:
                names = (
                    f"Adam MGold{year}",
                    f"Bruno MSilver{year}",
                    f"Chris MBronze{year}",
                )
                nat = "FRA"
            return {
                "Results": [
                    {"IsTeam": False, "Name": names[0], "Nat": nat, "Rank": "1"},
                    {"IsTeam": False, "Name": names[1], "Nat": nat, "Rank": "2"},
                    {"IsTeam": False, "Name": names[2], "Nat": nat, "Rank": "3"},
                ]
            }
        if disc == "RL":
            gold, silver, bronze = relay_medals.get(year, ("NOR", "SWE", "FRA"))
            return {
                "Results": [
                    {"IsTeam": True, "Nat": gold, "Rank": "1"},
                    {"IsTeam": True, "Nat": silver, "Rank": "2"},
                    {"IsTeam": True, "Nat": bronze, "Rank": "3"},
                ]
            }
        return {"Results": []}

    monkeypatch.setattr(brief, "get_race_results", fake_get_race_results)

    disciplines, rows_by_discipline = brief._build_previous_venue_podium_rows(
        upcoming_races,
        venue_events,
        reference_date=datetime.date(2026, 1, 10),
        exclude_event_ids={"EVT_CUR"},
        edition_limit=5,
    )

    assert disciplines == [("IN", "Individual"), ("RL", "Relay")]

    individual_rows = rows_by_discipline["IN"]
    relay_rows = rows_by_discipline["RL"]

    assert [row[0] for row in individual_rows] == [
        "2025-01-10",
        "2024-01-10",
        "2023-01-10",
        "2022-01-10",
        "2021-01-10",
    ]
    assert [row[0] for row in relay_rows] == [
        "2025-01-10",
        "2024-01-10",
        "2023-01-10",
        "2021-01-10",
        "2020-01-10",
    ]
    assert individual_rows[0][2:5] == [
        "A. WGold2025 (NOR)",
        "B. WSilver2025 (NOR)",
        "C. WBronze2025 (NOR)",
    ]
    assert individual_rows[0][5:8] == [
        "A. MGold2025 (FRA)",
        "B. MSilver2025 (FRA)",
        "C. MBronze2025 (FRA)",
    ]
    assert relay_rows[0][2:5] == ["Norway", "France", "Sweden"]
    assert relay_rows[0][5:8] == ["-", "-", "-"]
    assert relay_rows[3][2:5] == ["Germany", "Sweden", "Norway"]


def test_previous_venue_winner_rows_include_date_discipline_and_gender(monkeypatch):
    upcoming_races = [
        {
            "RaceId": "CUR_SW_IN",
            "catId": "SW",
            "DisciplineId": "IN",
            "StartTime": "2026-01-10T10:00:00Z",
        },
        {
            "RaceId": "CUR_SM_IN",
            "catId": "SM",
            "DisciplineId": "IN",
            "StartTime": "2026-01-10T12:00:00Z",
        },
        {
            "RaceId": "CUR_SW_RL",
            "catId": "SW",
            "DisciplineId": "RL",
            "StartTime": "2026-01-11T10:00:00Z",
        },
    ]
    venue_events = [
        {
            "event_id": "EVT_CUR",
            "season_id": "2526",
            "start_date": "2026-01-10",
            "event": {"Description": "BMW IBU World Cup"},
        },
        {
            "event_id": "EVT_2025",
            "season_id": "2425",
            "start_date": "2025-01-10",
            "event": {"Description": "BMW IBU World Cup"},
        },
        {
            "event_id": "EVT_2024",
            "season_id": "2324",
            "start_date": "2024-01-10",
            "event": {"Description": "BMW IBU World Cup"},
        },
        {
            "event_id": "EVT_2023",
            "season_id": "2223",
            "start_date": "2023-01-10",
            "event": {"Description": "BMW IBU World Cup"},
        },
        {
            "event_id": "EVT_2022",
            "season_id": "2122",
            "start_date": "2022-01-10",
            "event": {"Description": "BMW IBU World Cup"},
        },
        {
            "event_id": "EVT_2021",
            "season_id": "2021",
            "start_date": "2021-01-10",
            "event": {"Description": "BMW IBU World Cup"},
        },
        {
            "event_id": "EVT_2020",
            "season_id": "1920",
            "start_date": "2020-01-10",
            "event": {"Description": "BMW IBU World Cup"},
        },
    ]

    def fake_get_races(event_id: str):
        if event_id == "EVT_2025":
            return [
                {
                    "RaceId": "EVT_2025_SW_PU",
                    "catId": "SW",
                    "DisciplineId": "PU",
                    "StartTime": "2025-01-09T10:00:00Z",
                },
                {
                    "RaceId": "EVT_2025_SW_IN",
                    "catId": "SW",
                    "DisciplineId": "IN",
                    "StartTime": "2025-01-10T10:00:00Z",
                },
                {
                    "RaceId": "EVT_2025_SM_IN",
                    "catId": "SM",
                    "DisciplineId": "IN",
                    "StartTime": "2025-01-10T12:00:00Z",
                },
                {
                    "RaceId": "EVT_2025_SW_RL",
                    "catId": "SW",
                    "DisciplineId": "RL",
                    "StartTime": "2025-01-11T10:00:00Z",
                },
                {
                    "RaceId": "EVT_2025_SM_RL",
                    "catId": "SM",
                    "DisciplineId": "RL",
                    "StartTime": "2025-01-11T12:00:00Z",
                },
            ]
        if event_id == "EVT_2024":
            return [
                {
                    "RaceId": "EVT_2024_SW_SP",
                    "catId": "SW",
                    "DisciplineId": "SP",
                    "StartTime": "2024-01-09T10:00:00Z",
                },
                {
                    "RaceId": "EVT_2024_SM_SP",
                    "catId": "SM",
                    "DisciplineId": "SP",
                    "StartTime": "2024-01-09T12:00:00Z",
                },
                {
                    "RaceId": "EVT_2024_SW_IN",
                    "catId": "SW",
                    "DisciplineId": "IN",
                    "StartTime": "2024-01-10T10:00:00Z",
                },
                {
                    "RaceId": "EVT_2024_SM_IN",
                    "catId": "SM",
                    "DisciplineId": "IN",
                    "StartTime": "2024-01-10T12:00:00Z",
                },
                {
                    "RaceId": "EVT_2024_MX_RL",
                    "catId": "MX",
                    "DisciplineId": "RL",
                    "StartTime": "2024-01-11T10:00:00Z",
                },
                {
                    "RaceId": "EVT_2024_MX_SR",
                    "catId": "MX",
                    "DisciplineId": "SR",
                    "StartTime": "2024-01-12T10:00:00Z",
                },
            ]
        if event_id in {"EVT_2023", "EVT_2022", "EVT_2021", "EVT_2020"}:
            year = event_id.split("_")[1]
            return [
                {
                    "RaceId": f"{event_id}_SW_IN",
                    "catId": "SW",
                    "DisciplineId": "IN",
                    "StartTime": f"{year}-01-10T10:00:00Z",
                },
                {
                    "RaceId": f"{event_id}_SM_IN",
                    "catId": "SM",
                    "DisciplineId": "IN",
                    "StartTime": f"{year}-01-10T12:00:00Z",
                },
            ]
        return []

    monkeypatch.setattr(brief, "get_races", fake_get_races)

    def fake_get_race_results(race_id: str):
        parts = race_id.split("_")
        year = parts[1]
        cat = parts[2]
        disc = parts[3]
        if disc == "PU":
            winner = f"Alice WPursuit{year}" if cat == "SW" else f"Adam MPursuit{year}"
            nat = "NOR" if cat == "SW" else "FRA"
            return {
                "Results": [{"IsTeam": False, "Name": winner, "Nat": nat, "Rank": "1"}]
            }
        if disc == "SP":
            winner = f"Alice WSprint{year}" if cat == "SW" else f"Adam MSprint{year}"
            nat = "NOR" if cat == "SW" else "FRA"
            return {
                "Results": [{"IsTeam": False, "Name": winner, "Nat": nat, "Rank": "1"}]
            }
        if disc == "IN":
            winner = f"Alice WWinner{year}" if cat == "SW" else f"Adam MWinner{year}"
            nat = "NOR" if cat == "SW" else "FRA"
            return {
                "Results": [{"IsTeam": False, "Name": winner, "Nat": nat, "Rank": "1"}]
            }
        if disc == "RL":
            winner_nat = "SWE" if cat == "MX" else ("NOR" if cat == "SW" else "FRA")
            return {"Results": [{"IsTeam": True, "Nat": winner_nat, "Rank": "1"}]}
        if disc == "SR":
            winner_nat = "GER"
            return {"Results": [{"IsTeam": True, "Nat": winner_nat, "Rank": "1"}]}
        return {"Results": []}

    monkeypatch.setattr(brief, "get_race_results", fake_get_race_results)

    rows, _separators = brief._build_previous_venue_winner_rows(
        upcoming_races,
        venue_events,
        reference_date=datetime.date(2026, 1, 10),
        exclude_event_ids={"EVT_CUR"},
        edition_limit=5,
    )

    assert rows == [
        ["Pursuit", "2025-01-09", "A. WPursuit2025 (NOR)", "-", "-"],
        [
            "Individual",
            "2025-01-10",
            "A. WWinner2025 (NOR)",
            "2025-01-10",
            "A. MWinner2025 (FRA)",
        ],
        ["Relay", "2025-01-11", "NORWAY", "2025-01-11", "FRANCE"],
        [
            "Sprint",
            "2024-01-09",
            "A. WSprint2024 (NOR)",
            "2024-01-09",
            "A. MSprint2024 (FRA)",
        ],
        [
            "Individual",
            "2024-01-10",
            "A. WWinner2024 (NOR)",
            "2024-01-10",
            "A. MWinner2024 (FRA)",
        ],
        ["Mixed Relay", "2024-01-11", "SWEDEN", "2024-01-11", "SWEDEN"],
        ["Single Mixed Relay", "2024-01-12", "GERMANY", "2024-01-12", "GERMANY"],
        [
            "Individual",
            "2023-01-10",
            "A. WWinner2023 (NOR)",
            "2023-01-10",
            "A. MWinner2023 (FRA)",
        ],
        [
            "Individual",
            "2022-01-10",
            "A. WWinner2022 (NOR)",
            "2022-01-10",
            "A. MWinner2022 (FRA)",
        ],
        [
            "Individual",
            "2021-01-10",
            "A. WWinner2021 (NOR)",
            "2021-01-10",
            "A. MWinner2021 (FRA)",
        ],
    ]


def test_relay_athletes_cell_formatter_highlights_current_season_participants(
    monkeypatch,
):
    monkeypatch.setattr(brief.Color, "highlight_plain", lambda text: f"<H>{text}</H>")
    monkeypatch.setattr(brief.Color, "dim", lambda text: f"<D>{text}</D>")

    formatter = brief._relay_athletes_cell_formatter({"I TANDREVOLD"})

    assert (
        formatter("I. Tandrevold/K. Knotten", 0)
        == "<H>I. Tandrevold</H>/<D>K. Knotten</D>"
    )


def test_winner_name_cell_formatter_highlights_athlete_only(monkeypatch):
    monkeypatch.setattr(brief.Color, "highlight_plain", lambda text: f"<H>{text}</H>")
    formatter = brief._winner_name_cell_formatter({"ONE W", "I TANDREVOLD"})

    assert formatter("W. One (NOR)", 0) == "<H>W. One (NOR)</H>"
    assert (
        formatter("NORWAY (I. Tandrevold, K. Knotten)", 0)
        == "NORWAY (<H>I. Tandrevold</H>, K. Knotten)"
    )
    assert formatter("NORWAY", 0) == "NORWAY"


def test_winner_name_cell_formatter_dims_non_recent_athletes(monkeypatch):
    monkeypatch.setattr(brief.Color, "dim", lambda text: f"<D>{text}</D>")
    formatter = brief._winner_name_cell_formatter(
        highlight_name_keys=set(),
        recent_name_keys={"ONE W", "I TANDREVOLD"},
    )

    assert formatter("W. One (NOR)", 0) == "W. One (NOR)"
    assert formatter("A. Other (FRA)", 0) == "<D>A. Other (FRA)</D>"
    assert (
        formatter("NORWAY (I. Tandrevold, K. Knotten)", 0)
        == "NORWAY (I. Tandrevold, <D>K. Knotten</D>)"
    )


def test_extract_race_podium_cells_relay_includes_lineup():
    payload = {
        "Results": [
            {"IsTeam": True, "Bib": "7", "Nat": "NOR", "Rank": "1"},
            {"IsTeam": True, "Bib": "5", "Nat": "FRA", "Rank": "2"},
            {"IsTeam": True, "Bib": "9", "Nat": "SWE", "Rank": "3"},
            {
                "IsTeam": False,
                "Bib": "7",
                "Nat": "NOR",
                "Leg": "1",
                "Name": "Ingrid Tandrevold",
            },
            {
                "IsTeam": False,
                "Bib": "7",
                "Nat": "NOR",
                "Leg": "2",
                "Name": "Karoline Knotten",
            },
            {
                "IsTeam": False,
                "Bib": "7",
                "Nat": "NOR",
                "Leg": "3",
                "Name": "Juni Arnekleiv",
            },
            {
                "IsTeam": False,
                "Bib": "7",
                "Nat": "NOR",
                "Leg": "4",
                "Name": "Maren Kirkeeide",
            },
        ]
    }

    gold, silver, bronze = brief._extract_race_podium_cells(payload, is_team_race=True)

    assert gold == "Norway (I. Tandrevold, K. Knotten, J. Arnekleiv, M. Kirkeeide)"
    assert silver == "France"
    assert bronze == "Sweden"


def test_render_previous_podium_tables_splits_relay_gender_tables(monkeypatch, capsys):
    monkeypatch.setattr(
        brief,
        "_build_previous_venue_podium_rows",
        lambda *a, **k: (
            [("RL", "Relay"), ("IN", "Individual")],
            {
                "RL": [["2025-01-10", "WC", "WG", "WS", "WB", "MG", "MS", "MB"]],
                "IN": [["2025-01-10", "WC", "WG", "WS", "WB", "MG", "MS", "MB"]],
            },
        ),
    )

    table_calls: list[dict] = []

    def fake_render_table(headers, rows, **kwargs):
        table_calls.append({"headers": headers, "rows": rows, "kwargs": kwargs})

    monkeypatch.setattr(brief, "render_table", fake_render_table)

    brief._render_preevent_previous_podium_tables(
        "Kontiolahti",
        races=[],
        venue_events=[],
        reference_date=None,
        args=argparse.Namespace(format="tsv"),
        edition_limit=5,
    )

    out = capsys.readouterr().out
    assert "### Relay" in out
    assert "#### Women" in out
    assert "#### Men" in out
    assert "### Individual" in out

    assert len(table_calls) == 3
    assert table_calls[0]["headers"] == [
        "Edition",
        "Type",
        "Gold",
        "Silver",
        "Bronze",
    ]
    assert table_calls[0]["rows"] == [
        ["2025-01-10", "WC", "WG", "WS", "WB"],
        ["", "", "-", "-", "-"],
    ]
    assert table_calls[1]["headers"] == [
        "Edition",
        "Type",
        "Gold",
        "Silver",
        "Bronze",
    ]
    assert table_calls[1]["rows"] == [
        ["2025-01-10", "WC", "MG", "MS", "MB"],
        ["", "", "-", "-", "-"],
    ]
    assert table_calls[0]["kwargs"]["row_styles"] == ["", "dim"]
    assert table_calls[1]["kwargs"]["row_styles"] == ["", "dim"]
    assert table_calls[2]["headers"] == [
        "Edition",
        "Type",
        "Gold",
        "Silver",
        "Bronze",
        "Gold",
        "Silver",
        "Bronze",
    ]
    assert table_calls[2]["kwargs"]["group_headers"] == [(2, 5, "Women"), (5, 8, "Men")]


def test_render_previous_podium_tables_marks_first_venue_discipline(
    monkeypatch, capsys
):
    monkeypatch.setattr(
        brief,
        "_build_previous_venue_podium_rows",
        lambda *a, **k: ([("PU", "Pursuit")], {"PU": []}),
    )

    def fail_render_table(*args, **kwargs):
        raise AssertionError(
            "render_table should not be called for empty discipline history"
        )

    monkeypatch.setattr(brief, "render_table", fail_render_table)

    brief._render_preevent_previous_podium_tables(
        "Otepaa",
        races=[],
        venue_events=[],
        reference_date=None,
        args=argparse.Namespace(format="tsv"),
        edition_limit=5,
    )

    out = capsys.readouterr().out
    assert "### Pursuit" in out
    assert "This will be the first pursuit in Otepaa history." in out
    assert "\nnone\n" not in out


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


def test_build_venue_decorated_athlete_rows_prefers_strict_gender_votes(monkeypatch):
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
                {"RaceId": "R_MX", "catId": "MX", "DisciplineId": "SR"},
                {"RaceId": "R_SW", "catId": "SW", "DisciplineId": "SP"},
            ]
            if event_id == "EVT1"
            else []
        ),
    )
    payloads = {
        "R_MX": {
            "Results": [
                {"IsTeam": True, "Bib": "7", "Nat": "FRA", "Rank": "1"},
                {
                    "IsTeam": False,
                    "IBUId": "F1",
                    "Name": "SIMON Julia",
                    "Nat": "FRA",
                    "Bib": "7",
                    "Leg": "2",
                },
            ]
        },
        "R_SW": {
            "Results": [
                {
                    "IsTeam": False,
                    "IBUId": "F1",
                    "Name": "Julia SIMON",
                    "Nat": "FRA",
                    "Rank": "1",
                }
            ]
        },
    }
    monkeypatch.setattr(brief, "get_race_results", lambda race_id: payloads[race_id])

    rows, _row_styles = brief._build_venue_decorated_athlete_rows(
        "Kontiolahti", limit=10
    )

    simon = next(row for row in rows if row[1] in {"SIMON Julia", "Julia SIMON"})
    assert simon[2] == "FRA"
    assert simon[3] == "F"


def test_build_venue_decorated_athlete_rows_excludes_specific_race(monkeypatch):
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
                {"RaceId": "R_OLD", "catId": "SW", "DisciplineId": "SP"},
                {"RaceId": "R_TARGET", "catId": "SW", "DisciplineId": "SP"},
            ]
            if event_id == "EVT1"
            else []
        ),
    )

    fetched_race_ids: list[str] = []
    payloads = {
        "R_OLD": {
            "Results": [
                {
                    "IsTeam": False,
                    "IBUId": "W1",
                    "Name": "Old Winner",
                    "Nat": "NOR",
                    "Rank": "1",
                }
            ]
        },
        "R_TARGET": {
            "Results": [
                {
                    "IsTeam": False,
                    "IBUId": "W2",
                    "Name": "Target Winner",
                    "Nat": "SWE",
                    "Rank": "1",
                }
            ]
        },
    }

    def fake_get_race_results(race_id: str) -> dict:
        fetched_race_ids.append(race_id)
        return payloads[race_id]

    monkeypatch.setattr(brief, "get_race_results", fake_get_race_results)

    rows, _styles = brief._build_venue_decorated_athlete_rows(
        "Kontiolahti",
        limit=10,
        exclude_race_ids={"R_TARGET"},
    )

    assert any(row[1] == "Old Winner" for row in rows)
    assert all(row[1] != "Target Winner" for row in rows)
    assert "R_TARGET" not in fetched_race_ids


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


def test_build_decorated_rows_highlight_name_alias_when_current_key_differs(
    monkeypatch,
):
    events = [
        {"event_id": "OLD_EVT", "season_id": "2425"},
        {"event_id": "CUR_EVT", "season_id": "2526"},
    ]
    monkeypatch.setattr(
        brief,
        "get_races",
        lambda event_id: (
            [{"RaceId": "R_OLD", "catId": "SM", "DisciplineId": "SP"}]
            if event_id == "OLD_EVT"
            else (
                [{"RaceId": "R_CUR", "catId": "SM", "DisciplineId": "SP"}]
                if event_id == "CUR_EVT"
                else []
            )
        ),
    )
    payloads = {
        "R_OLD": {
            "Results": [
                {
                    "IsTeam": False,
                    "Name": "DOE John",
                    "Nat": "GER",
                    "Rank": "1",
                },
                {
                    "IsTeam": False,
                    "Name": "Legacy Guy",
                    "Nat": "NOR",
                    "Rank": "2",
                },
            ]
        },
        "R_CUR": {
            "Results": [
                {
                    "IsTeam": False,
                    "IBUId": "CUR_JOHN",
                    "Name": "John DOE",
                    "Nat": "GER",
                    "Rank": "3",
                }
            ]
        },
    }
    monkeypatch.setattr(brief, "get_race_results", lambda race_id: payloads[race_id])

    rows, row_styles = brief._build_decorated_athlete_rows_for_events(
        events,
        current_season_id="2526",
        limit=0,
    )

    styles_by_name = {row[1]: row_styles[idx] for idx, row in enumerate(rows)}
    assert styles_by_name["DOE John"] == "highlight_plain"
    assert styles_by_name["John DOE"] == "highlight_plain"
    assert styles_by_name["Legacy Guy"] == ""


def test_build_venue_decorated_athlete_rows_single_mixed_relay_counts_medal_once(
    monkeypatch,
):
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
            [{"RaceId": "R_MX", "catId": "MX", "DisciplineId": "SR"}]
            if event_id == "EVT1"
            else []
        ),
    )
    monkeypatch.setattr(
        brief,
        "get_race_results",
        lambda race_id: {
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
                {
                    "IsTeam": False,
                    "IBUId": "F1",
                    "Name": "Fiona",
                    "Nat": "FRA",
                    "Bib": "7",
                    "Leg": "3",
                },
                {
                    "IsTeam": False,
                    "IBUId": "M1",
                    "Name": "Marc",
                    "Nat": "FRA",
                    "Bib": "7",
                    "Leg": "4",
                },
            ]
        },
    )

    rows, _styles = brief._build_venue_decorated_athlete_rows("Kontiolahti", limit=10)

    fiona = next(row for row in rows if row[1] == "Fiona")
    marc = next(row for row in rows if row[1] == "Marc")

    assert fiona[4:9] == ["1", "0", "0", "1", "1"]
    assert fiona[14:19] == ["1", "0", "0", "1", "1"]
    assert marc[4:9] == ["1", "0", "0", "1", "1"]
    assert marc[14:19] == ["1", "0", "0", "1", "1"]


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


def test_render_decorated_tables_add_blank_lines_between_headers_and_sections(
    monkeypatch, capsys
):
    monkeypatch.setattr(
        brief, "render_table", lambda headers, rows, **kwargs: print("TABLE")
    )

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
    ]

    brief._render_decorated_athletes_split_tables(
        "Most Decorated Athletes at Kontiolahti",
        rows,
        ["", ""],
        argparse.Namespace(format="tsv"),
    )

    out = capsys.readouterr().out
    assert out.startswith(
        "## Most Decorated Athletes at Kontiolahti\n\n### Women\n\nTABLE\n\n\n### Men\n\nTABLE\n\n\n"
    )


def test_render_decorated_tables_can_filter_to_single_gender(capsys):
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
    ]

    brief._render_decorated_athletes_split_tables(
        "Most Decorated Athletes at Kontiolahti",
        rows,
        ["", ""],
        argparse.Namespace(format="tsv"),
        gender_filter="M",
    )

    out = capsys.readouterr().out
    assert "### Women" not in out
    assert "### Men" in out
    assert "Woman A" not in out
    assert "Man A" in out


def test_render_decorated_tables_add_info_group_header(monkeypatch):
    captured: dict[str, object] = {}

    def fake_render_table(headers, rows, **kwargs):
        captured["headers"] = headers
        captured["kwargs"] = kwargs

    monkeypatch.setattr(brief, "render_table", fake_render_table)

    brief._render_decorated_athletes_split_tables(
        "Most Decorated Athletes at Kontiolahti",
        [
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
            ]
        ],
        [""],
        argparse.Namespace(format="tsv"),
        gender_filter="F",
    )

    assert captured["headers"][:3] == ["#", "Athlete", "Nat"]
    assert captured["kwargs"]["group_headers"] == [
        (0, 3, "Info"),
        (3, 8, "All"),
        (8, 13, "Individual"),
        (13, 18, "Team"),
    ]


def test_render_decorated_tables_pretty_uses_dim_upper_group_headers(
    monkeypatch, capsys
):
    monkeypatch.setattr(brief.Color, "dim", lambda text: f"<DIM>{text}</DIM>")

    brief._render_decorated_athletes_split_tables(
        "Most Decorated Athletes at Kontiolahti",
        [
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
            ]
        ],
        [""],
        argparse.Namespace(format="pretty"),
        gender_filter="F",
    )

    out = capsys.readouterr().out
    assert "<DIM>INFO</DIM>" in out
    assert "<DIM>ALL</DIM>" in out
    assert "<DIM>INDIVIDUAL</DIM>" in out
    assert "<DIM>TEAM</DIM>" in out


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
    assert out.startswith("\n# Event Brief - Ruhpolding\n\n## Event Facts\n\n")
    assert "## Event Facts\n\nCountry\tWC Editions\tWCH Editions\tOWG Editions" in out
    assert (
        "## Event Facts\n\nCountry\tWC Editions\tWCH Editions\tOWG Editions\nGermany\t2\t0\t1\n\n\n## Event Agenda\n\n"
        in out
    )
    assert "Germany\t2\t0\t1" in out
    assert "## Last 10 Editions at Ruhpolding" in out
    assert "## Previous Winners at Ruhpolding" in out
    assert "## Previous Podiums at Ruhpolding" in out
    assert out.index("## Event Agenda") < out.index("## Last 10 Editions at Ruhpolding")
    assert out.index("## Last 10 Editions at Ruhpolding") < out.index(
        "## Previous Winners at Ruhpolding"
    )
    assert out.index("## Previous Winners at Ruhpolding") < out.index(
        "## Previous Podiums at Ruhpolding"
    )
    assert out.index("## Previous Podiums at Ruhpolding") < out.index(
        "## Athlete Standings"
    )
    assert (
        "Edition\tType\tSprint\tPursuit\tIndividual\tMass Start\tRelay\tMixed Relay\tSingle Mixed Relay"
        in out
    )
    assert "2026-01-10\tWC\tX\t-\t-\t-\t-\t-\t-" in out
    assert "### Sprint" in out
    assert (
        "## Previous Winners at Ruhpolding\n\nDiscipline\tDate\tWinner\tDate\tWinner"
        in out
    )
    assert "Sprint\t2026-01-01\tW. One (NOR)\t2026-01-01\tM. One (FRA)" in out
    assert "Mixed Relay\t2026-01-03\tSWEDEN\t2026-01-03\tSWEDEN" in out
    assert "Edition\tType\tGold\tSilver\tBronze\tGold\tSilver\tBronze" in out
    assert (
        "### Sprint\n\nEdition\tType\tGold\tSilver\tBronze\tGold\tSilver\tBronze" in out
    )
    assert "2025-12-30\tWC\tW. One (NOR)\t-\t-\tM. One (FRA)\t-\t-" in out
    assert "2026-02-01\tWCH" not in out
    assert "## Event Agenda" in out
    assert "## Athlete Standings" in out
    assert "## Relay Standings" in out
    assert "## Nations Cup Standings" in out
    assert "## Most Decorated Athletes at Ruhpolding" in out
    assert "## Most Decorated Athletes at World Cup" in out
    assert "## Most Decorated Athletes at major events (WC+WCH+OWG)" in out
    assert out.rfind("## Most Decorated Athletes at World Cup") > out.rfind(
        "## Nations Cup Standings"
    )
    assert out.rfind(
        "## Most Decorated Athletes at major events (WC+WCH+OWG)"
    ) > out.rfind("## Most Decorated Athletes at World Cup")
    assert (
        "#\tAthlete\tNat\tGold\tSilver\tBronze\tTotal\tRaces\tGold\tSilver\tBronze\tTotal\tRaces\tGold\tSilver\tBronze\tTotal\tRaces"
        in out
    )
    assert "### Women" in out
    assert "### Men" in out
    assert "Date\tDay\tTime\tCategory\tDiscipline\tSeason Race\tSeason Race Full" in out
    assert out.count("\t2/2\t2/2\n") >= 2
    assert "### World Cup Total Score" in out
    assert "### World Cup Sprint Score" in out
    assert "### World Cup Pursuit Score" in out
    assert "### World Cup Individual Score" in out
    assert "### World Cup Mass Start Score" in out
    assert "Rank\tAthlete\tNat\tAge\tPoints" in out
    assert "### U23" not in out
    assert "Overall - Women" not in out
    assert "Overall - Men" not in out
    assert "Mixed Relay" in out
    assert "Women" in out
    assert "Men" in out


def test_handle_brief_preevent_excludes_current_event_from_decorated_sections(
    monkeypatch,
):
    monkeypatch.setattr(
        brief,
        "_find_event_by_id",
        lambda event_id: {
            "EventId": event_id,
            "SeasonId": "2526",
            "Organizer": "Ruhpolding",
            "Description": "BMW IBU World Cup",
            "Nat": "GER",
        },
    )
    monkeypatch.setattr(
        brief,
        "get_races",
        lambda event_id: [
            {
                "RaceId": "TARGET",
                "catId": "SW",
                "DisciplineId": "SP",
                "StartTime": "2026-01-10T10:00:00Z",
            }
        ],
    )
    monkeypatch.setattr(
        brief,
        "get_race_results",
        lambda race_id: {
            "SportEvt": {"Description": "BMW IBU World Cup", "Organizer": "Ruhpolding"},
            "Competition": {
                "DisciplineId": "SP",
                "catId": "SW",
                "StartTime": "2026-01-10T10:00:00Z",
            },
            "Results": [],
        },
    )
    monkeypatch.setattr(brief, "_collect_venue_level1_events", lambda venue_name: [])
    monkeypatch.setattr(
        brief,
        "_count_venue_event_editions",
        lambda *a, **k: (0, 0, 0),
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
    monkeypatch.setattr(brief, "_render_preevent_agenda", lambda *a, **k: None)
    monkeypatch.setattr(brief, "_build_recent_venue_edition_rows", lambda *a, **k: [])
    monkeypatch.setattr(
        brief, "_collect_current_season_participant_keys", lambda *a, **k: set()
    )

    captured: dict[str, set[str] | None] = {
        "venue": None,
        "event_type": None,
        "major": None,
    }

    def fake_build_venue(*args, **kwargs):
        captured["venue"] = kwargs.get("exclude_event_ids")
        return [], []

    def fake_build_event_type(*args, **kwargs):
        captured["event_type"] = kwargs.get("exclude_event_ids")
        return [], []

    def fake_build_major(*args, **kwargs):
        captured["major"] = kwargs.get("exclude_event_ids")
        return [], []

    monkeypatch.setattr(brief, "_build_venue_decorated_athlete_rows", fake_build_venue)
    monkeypatch.setattr(
        brief, "_build_event_type_decorated_athlete_rows", fake_build_event_type
    )
    monkeypatch.setattr(
        brief, "_build_major_events_decorated_athlete_rows", fake_build_major
    )
    monkeypatch.setattr(
        brief,
        "_render_decorated_athletes_split_tables",
        lambda *a, **k: None,
    )

    rc = brief.handle_brief_preevent(argparse.Namespace(event="EVT1", format="tsv"))

    assert rc == 0
    assert captured["venue"] == {"EVT1"}
    assert captured["event_type"] == {"EVT1"}
    assert captured["major"] == {"EVT1"}


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


def test_render_preevent_agenda_sets_column_separators_for_wc(monkeypatch):
    captured: list[tuple[list[str], list[list[str]], dict]] = []

    def fake_render_table(headers, rows, **kwargs):
        captured.append((headers, rows, kwargs))

    monkeypatch.setattr(brief, "render_table", fake_render_table)
    monkeypatch.setattr(
        brief,
        "_build_season_race_sequence_maps",
        lambda *a, **k: ({("SW", "R1"): "1/9"}, {("SW", "R1"): "1/17"}),
    )

    brief._render_preevent_agenda(
        [
            {
                "RaceId": "R1",
                "catId": "SW",
                "DisciplineId": "SP",
                "StartTime": "2026-02-10T13:05:00Z",
            }
        ],
        argparse.Namespace(format="pretty"),
        season_id="2526",
        event_type=brief.EVENT_TYPE_WC,
        event_id="EVT1",
        level=1,
    )

    assert captured
    _headers, _rows, kwargs = captured[0]
    assert kwargs.get("column_separators") == {3, 5}


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


def test_snapshot_athlete_standings_pretty_adds_u23_marker(monkeypatch, capsys):
    monkeypatch.setattr(brief.Color, "enabled", classmethod(lambda cls: True))
    monkeypatch.setattr(
        brief.Color, "dark_blue", lambda text, bold=False: f"<U23>{text}</U23>"
    )
    monkeypatch.setattr(brief, "_prefetch_bios", lambda ibu_ids: {})

    total_rows = [
        {"Rank": 1, "IBUId": "A", "Name": "Leader", "Nat": "NOR", "Score": 130},
        {
            "Rank": 2,
            "IBUId": "B",
            "Name": "Youngster",
            "Nat": "FRA",
            "Score": 120,
            "Groups": "U23",
        },
    ]
    discipline_rows = {"SP": [], "PU": [], "IN": [], "MS": []}

    brief._render_snapshot_athlete_standings_table(
        "Women",
        total_rows,
        discipline_rows,
        argparse.Namespace(format=""),
        reference_date=datetime.date(2026, 1, 10),
        u23_cutoff_year=2003,
    )

    out = capsys.readouterr().out
    assert "<U23>●</U23>" in out


def test_snapshot_athlete_standings_pretty_only_marks_overall_u23_leader(
    monkeypatch, capsys
):
    monkeypatch.setattr(brief, "_prefetch_bios", lambda ibu_ids: {})

    def fake_render_table(headers, rows, **kwargs):
        print(" | ".join(headers))
        for row in rows:
            print(" | ".join(str(cell) for cell in row))

    monkeypatch.setattr(brief, "render_table", fake_render_table)

    total_rows = [
        {"Rank": 1, "IBUId": "A", "Name": "Leader", "Nat": "NOR", "Score": 130},
        {
            "Rank": 2,
            "IBUId": "B",
            "Name": "Overall U23",
            "Nat": "FRA",
            "Score": 120,
            "Groups": "U23",
            "BestU23": True,
        },
        {
            "Rank": 3,
            "IBUId": "C",
            "Name": "Sprint U23",
            "Nat": "GER",
            "Score": 110,
            "Groups": "U23",
        },
    ]
    discipline_rows = {
        "SP": [
            {
                "Rank": 1,
                "IBUId": "C",
                "Name": "Sprint U23",
                "Nat": "GER",
                "Score": 70,
                "Groups": "U23",
                "BestU23": True,
            }
        ],
        "PU": [],
        "IN": [],
        "MS": [],
    }

    brief._render_snapshot_athlete_standings_table(
        "Women",
        total_rows,
        discipline_rows,
        argparse.Namespace(format="pretty"),
        u23_cutoff_year=2003,
    )

    out = capsys.readouterr().out
    assert f"Overall U23 {brief.U23_LEADER_MARKER}" in out
    assert f"Sprint U23 {brief.U23_LEADER_MARKER}" not in out


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
    assert "\t24\t120 (-10)\t" in out


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
    assert f"\t{expected_sp} (-60)\t{expected_pu}\t" in out


def test_snapshot_athlete_standings_u23_uses_incremental_rank_and_wc_column(
    monkeypatch, capsys
):
    monkeypatch.setattr(brief, "_prefetch_bios", lambda ibu_ids: {})

    total_rows = [
        {"Rank": 1, "IBUId": "A", "Name": "Leader", "Nat": "NOR", "Score": 130},
        {
            "Rank": 3,
            "IBUId": "B",
            "Name": "Bravo",
            "Nat": "FRA",
            "Score": 90,
            "Groups": "U23",
        },
        {
            "Rank": 5,
            "IBUId": "C",
            "Name": "Charlie",
            "Nat": "GER",
            "Score": 70,
            "Groups": "U23",
        },
    ]
    discipline_rows = {"SP": [], "PU": [], "IN": [], "MS": []}

    brief._render_snapshot_athlete_standings_table(
        "Women",
        total_rows,
        discipline_rows,
        argparse.Namespace(format="tsv"),
    )

    out = capsys.readouterr().out
    assert "#### U23" in out
    assert "Rank\tWC\tAthlete\tNat\tPoints" in out
    assert "1\t3\tBravo\tFRA\t90" in out
    assert "2\t5\tCharlie\tGER\t70" in out


def test_snapshot_athlete_standings_pretty_renders_women_and_men_side_by_side(
    monkeypatch, capsys
):
    monkeypatch.setattr(brief, "_prefetch_bios", lambda ibu_ids: {})

    def fake_render_table(headers, rows, **kwargs):
        print(" | ".join(headers))
        for row in rows:
            print(" | ".join(str(cell) for cell in row))

    monkeypatch.setattr(brief, "render_table", fake_render_table)

    women_rows = {
        "TS": [
            {
                "Rank": 1,
                "IBUId": "WA",
                "Name": "Women Leader",
                "Nat": "NOR",
                "Score": 100,
                "Groups": "U23",
            }
        ],
        "SP": [],
        "PU": [],
        "IN": [],
        "MS": [],
    }
    men_rows = {
        "TS": [
            {
                "Rank": 2,
                "IBUId": "MB",
                "Name": "Men Leader",
                "Nat": "FRA",
                "Score": 90,
                "Groups": "U23",
            }
        ],
        "SP": [],
        "PU": [],
        "IN": [],
        "MS": [],
    }

    brief._render_snapshot_athlete_standings_tables(
        [("Women", women_rows), ("Men", men_rows)],
        argparse.Namespace(format="pretty"),
        u23_cutoff_year=2003,
    )

    out = capsys.readouterr().out
    assert "WOMEN" in out
    assert "MEN" in out
    assert "│" in out
    assert "### U23" in out
    assert "Rank | WC | Athlete | Nat | Points" in out


def test_snapshot_athlete_standings_sections_render_per_discipline_pairs(
    monkeypatch, capsys
):
    monkeypatch.setattr(brief, "_prefetch_bios", lambda ibu_ids: {})

    def fake_render_table(headers, rows, **kwargs):
        print(" | ".join(headers))
        for row in rows:
            print(" | ".join(str(cell) for cell in row))

    monkeypatch.setattr(brief, "render_table", fake_render_table)

    athlete_rows = {
        "SW": {
            "TS": [
                {
                    "Rank": 1,
                    "IBUId": "WA",
                    "Name": "Women Total",
                    "Nat": "NOR",
                    "Score": 100,
                    "Groups": "U23",
                }
            ],
            "SP": [
                {
                    "Rank": 2,
                    "IBUId": "WB",
                    "Name": "Women Sprint",
                    "Nat": "FRA",
                    "Score": 70,
                    "Groups": "U23",
                }
            ],
            "PU": [],
            "IN": [],
            "MS": [],
        },
        "SM": {
            "TS": [
                {
                    "Rank": 1,
                    "IBUId": "MA",
                    "Name": "Men Total",
                    "Nat": "SWE",
                    "Score": 90,
                    "Groups": "U23",
                }
            ],
            "SP": [
                {
                    "Rank": 3,
                    "IBUId": "MB",
                    "Name": "Men Sprint",
                    "Nat": "GER",
                    "Score": 60,
                    "Groups": "U23",
                }
            ],
            "PU": [],
            "IN": [],
            "MS": [],
        },
    }

    brief._render_snapshot_athlete_standings_sections(
        athlete_rows,
        argparse.Namespace(format="pretty"),
        u23_cutoff_year=2003,
    )

    out = capsys.readouterr().out
    assert "### World Cup Total Score" in out
    assert "### World Cup Sprint Score" in out
    assert "### World Cup Pursuit Score" in out
    assert "WOMEN" in out
    assert "MEN" in out
    assert "U23 WOMEN" in out
    assert "U23 MEN" in out
    assert "Rank | Athlete | Nat | Age | Points" in out
    assert "Rank | WC | Athlete | Nat | Points" in out
    assert "### U23" not in out


def test_snapshot_athlete_standings_sections_only_mark_overall_u23_leader(
    monkeypatch, capsys
):
    monkeypatch.setattr(brief, "_prefetch_bios", lambda ibu_ids: {})

    def fake_render_table(headers, rows, **kwargs):
        print(" | ".join(headers))
        for row in rows:
            print(" | ".join(str(cell) for cell in row))

    monkeypatch.setattr(brief, "render_table", fake_render_table)

    athlete_rows = {
        "SW": {
            "TS": [
                {
                    "Rank": 1,
                    "IBUId": "A",
                    "Name": "Leader",
                    "Nat": "NOR",
                    "Score": 130,
                },
                {
                    "Rank": 2,
                    "IBUId": "B",
                    "Name": "Overall U23",
                    "Nat": "FRA",
                    "Score": 120,
                    "Groups": "U23",
                    "BestU23": True,
                },
                {
                    "Rank": 3,
                    "IBUId": "C",
                    "Name": "Sprint U23",
                    "Nat": "GER",
                    "Score": 110,
                    "Groups": "U23",
                },
            ],
            "SP": [
                {
                    "Rank": 1,
                    "IBUId": "C",
                    "Name": "Sprint U23",
                    "Nat": "GER",
                    "Score": 70,
                    "Groups": "U23",
                    "BestU23": True,
                }
            ],
            "PU": [],
            "IN": [],
            "MS": [],
        },
        "SM": {},
    }

    brief._render_snapshot_athlete_standings_sections(
        athlete_rows,
        argparse.Namespace(format="pretty"),
        u23_cutoff_year=2003,
    )

    out = capsys.readouterr().out
    assert f"Overall U23 {brief.U23_LEADER_MARKER}" in out
    assert f"Sprint U23 {brief.U23_LEADER_MARKER}" not in out


def test_snapshot_athlete_standings_sections_bold_u23_discipline_leader(
    monkeypatch, capsys
):
    monkeypatch.setattr(brief.Color, "enabled", classmethod(lambda cls: True))
    monkeypatch.setattr(brief, "_prefetch_bios", lambda ibu_ids: {})

    athlete_rows = {
        "SW": {
            "TS": [
                {
                    "Rank": 1,
                    "IBUId": "L",
                    "Name": "Leader",
                    "Nat": "NOR",
                    "Score": 200,
                },
                {
                    "Rank": 2,
                    "IBUId": "O",
                    "Name": "Overall U23",
                    "Nat": "FRA",
                    "Score": 150,
                    "Groups": "U23",
                    "BestU23": True,
                },
                {
                    "Rank": 3,
                    "IBUId": "U",
                    "Name": "Sprint U23",
                    "Nat": "GER",
                    "Score": 140,
                    "Groups": "U23",
                },
            ],
            "SP": [
                {
                    "Rank": 1,
                    "IBUId": "S",
                    "Name": "Sprint Leader",
                    "Nat": "SWE",
                    "Score": 100,
                },
                {
                    "Rank": 2,
                    "IBUId": "U",
                    "Name": "Sprint U23",
                    "Nat": "GER",
                    "Score": 90,
                    "Groups": "U23",
                    "BestU23": True,
                },
                {
                    "Rank": 3,
                    "IBUId": "O",
                    "Name": "Overall U23",
                    "Nat": "FRA",
                    "Score": 80,
                    "Groups": "U23",
                },
            ],
            "PU": [],
            "IN": [],
            "MS": [],
        },
        "SM": {},
    }

    brief._render_snapshot_athlete_standings_sections(
        athlete_rows,
        argparse.Namespace(format="pretty"),
        u23_cutoff_year=2003,
    )

    out = capsys.readouterr().out
    assert f"{brief.Color.BOLD}Sprint U23{brief.Color.RESET}" in out
    assert f"{brief.Color.BOLD}Overall U23{brief.Color.RESET}" not in out


def test_snapshot_standings_excludes_non_counting_major_events(monkeypatch):
    monkeypatch.setattr(
        brief,
        "get_events",
        lambda season_id, level: [
            {"EventId": "E_WC", "Description": "BMW IBU World Cup"},
            {"EventId": "E_WCH", "Description": "BMW IBU World Championships"},
            {"EventId": "E_OWG", "Description": "Olympic Winter Games"},
        ],
    )
    monkeypatch.setattr(
        brief,
        "get_races",
        lambda event_id: [
            {
                "RaceId": f"{event_id}_R1",
                "catId": "SW",
                "DisciplineId": "SP",
                "StartTime": "2026-01-01T10:00:00Z",
            }
        ],
    )
    monkeypatch.setattr(
        brief,
        "get_race_results",
        lambda race_id: {
            "Results": [
                {
                    "IsTeam": False,
                    "IBUId": race_id[:-3] if race_id.endswith("_R1") else race_id,
                    "Name": race_id[:-3] if race_id.endswith("_R1") else race_id,
                    "Nat": "NOR",
                    "Rank": "1",
                }
            ]
        },
    )

    standings = brief._compute_preevent_snapshot_standings(
        "2526",
        target_race_id="",
        cutoff_dt=datetime.datetime(2026, 1, 10, tzinfo=datetime.timezone.utc),
        limit=10,
    )

    assert [row["IBUId"] for row in standings["athlete"]["SW"]["TS"]] == ["E_WC"]


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
    assert "WOMEN RELAY" in out
    assert "MEN RELAY" in out
    assert "MIXED RELAY" in out
    assert "ALL RELAY (UNOFFICIAL)" in out
    assert out.count("Rank") >= 3
    assert "NORWAY" in out and "FRANCE" in out and "SWEDEN" in out
    assert "Nat" not in out


def test_render_relay_tables_pretty_bolds_leaders(monkeypatch, capsys):
    monkeypatch.setattr(brief.Color, "enabled", classmethod(lambda cls: True))

    brief._render_relay_tables(
        {
            "SW": [{"Rank": 1, "Name": "Alpha Relay", "Nat": "FRA", "Score": 101}],
            "SM": [{"Rank": 1, "Name": "Bravo Relay", "Nat": "NOR", "Score": 202}],
            "MX": [{"Rank": 1, "Name": "Charlie Relay", "Nat": "ITA", "Score": 303}],
            "ALL": [{"Rank": 1, "Name": "Delta Relay", "Nat": "GER", "Score": 404}],
        },
        argparse.Namespace(format="pretty"),
    )

    out = capsys.readouterr().out
    assert f"{brief.Color.BOLD}FRANCE{brief.Color.RESET}" in out
    assert f"{brief.Color.BOLD}NORWAY{brief.Color.RESET}" in out
    assert f"{brief.Color.BOLD}ITALY{brief.Color.RESET}" in out
    assert f"{brief.Color.BOLD}GERMANY{brief.Color.RESET}" in out
    assert f"{brief.Color.BOLD}101{brief.Color.RESET}" in out
    assert f"{brief.Color.BOLD}202{brief.Color.RESET}" in out


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
    assert "1\tNORWAY\t265" in out
    assert "2\tFRANCE\t80 (-20)" in out


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
    assert "WOMEN" in out
    assert "MEN" in out
    assert "COMBINED (UNOFFICIAL)" in out
    assert "FRANCE" in out and "NORWAY" in out
    assert "France" not in out and "Norway" not in out
    assert out.count("Team") >= 2
    assert "1000.0" in out
    assert "995.0" in out
    assert "1995.0" not in out
    assert "1995" in out


def test_render_nations_tables_pretty_bolds_leaders(monkeypatch, capsys):
    monkeypatch.setattr(brief.Color, "enabled", classmethod(lambda cls: True))

    brief._render_nations_tables(
        {
            "SW": [{"Rank": 1, "Name": "FRANCE", "Nat": "FRA", "Score": 1111}],
            "SM": [{"Rank": 1, "Name": "NORWAY", "Nat": "NOR", "Score": 2222}],
            "ALL": [{"Rank": 1, "Name": "SWEDEN", "Nat": "SWE", "Score": 3333}],
        },
        argparse.Namespace(format="pretty"),
    )

    out = capsys.readouterr().out
    assert f"{brief.Color.BOLD}FRANCE{brief.Color.RESET}" in out
    assert f"{brief.Color.BOLD}NORWAY{brief.Color.RESET}" in out
    assert f"{brief.Color.BOLD}SWEDEN{brief.Color.RESET}" in out
    assert f"{brief.Color.BOLD}1111.0{brief.Color.RESET}" in out
    assert f"{brief.Color.BOLD}2222.0{brief.Color.RESET}" in out
    assert f"{brief.Color.BOLD}3333{brief.Color.RESET}" in out


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
    assert "1\tFRANCE\tFRA\t1995.0" in out
    assert "2\tNORWAY\tNOR\t900.5 (-1094.5)" in out


def test_preevent_points_gap_formatter_uses_muted_red(monkeypatch):
    monkeypatch.setattr(brief.Color, "enabled", classmethod(lambda cls: True))
    monkeypatch.setattr(brief.Color, "muted_red", lambda text: f"<gap>{text}</gap>")

    formatter = brief._standings_points_cell_formatter(
        ["130", "120 (-10)"],
        pretty=True,
    )

    assert formatter("120 (-10)", 1) == "120<gap> (-10)</gap>"


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

    assert "CZECH REPUBLIC" in relay_out
    assert "CZECH REPUBLIC" in nations_out
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
        lambda event_id: (
            [
                {
                    "RaceId": "TARGET",
                    "catId": "SW",
                    "DisciplineId": "SP",
                    "StartTime": "2099-01-10T10:00:00Z",
                }
            ]
            if event_id == "EVT1"
            else []
        ),
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
    assert "### World Cup Total Score" in out
    assert "### World Cup Sprint Score" in out
    assert "### World Cup Pursuit Score" in out
    assert "### World Cup Individual Score" in out
    assert "### World Cup Mass Start Score" in out
    assert "1\tCup Leader\tNOR\t-\t300" in out
    assert "1\tCup Leader\tNOR\t-\t120" in out
    assert "1\tCup Leader\tNOR\t-\t100" in out
    assert "1\tCup Leader\tNOR\t-\t80" in out
