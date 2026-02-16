"""Tests for the achievements command handler."""

import argparse

from biathlon.commands import achievements


def _args(**overrides) -> argparse.Namespace:
    base = {
        "men": False,
        "country": False,
        "nationality": "",
        "olympics": False,
        "world": False,
        "season": "",
        "limit": 50,
        "format": "tsv",
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def _mock_world_cup_dataset(monkeypatch) -> None:
    monkeypatch.setattr(
        achievements, "get_seasons", lambda: [{"SeasonId": "2526", "IsCurrent": True}]
    )
    monkeypatch.setattr(achievements, "get_current_season_id", lambda: "2526")
    monkeypatch.setattr(
        achievements,
        "get_events",
        lambda season_id, level: [
            {"EventId": "EWC", "Description": "BMW IBU World Cup Nove Mesto"}
        ],
    )
    monkeypatch.setattr(
        achievements,
        "get_races",
        lambda event_id: [
            {
                "RaceId": "RIND",
                "DisciplineId": "SP",
                "catId": "SW",
                "StartTime": "2026-01-01T10:00:00Z",
            },
            {
                "RaceId": "RREL",
                "DisciplineId": "RL",
                "catId": "SW",
                "StartTime": "2026-01-02T10:00:00Z",
            },
            {
                "RaceId": "RMIX",
                "DisciplineId": "MR",
                "catId": "MX",
                "StartTime": "2026-01-03T10:00:00Z",
            },
        ],
    )

    payloads = {
        "RIND": {
            "Competition": {"DisciplineId": "SP", "catId": "SW"},
            "Results": [
                {
                    "IsTeam": False,
                    "Rank": "1",
                    "IBUId": "W1",
                    "Name": "Alice",
                    "Nat": "NOR",
                },
                {
                    "IsTeam": False,
                    "Rank": "2",
                    "IBUId": "W2",
                    "Name": "Beth",
                    "Nat": "FRA",
                },
                {
                    "IsTeam": False,
                    "Rank": "3",
                    "IBUId": "W3",
                    "Name": "Cara",
                    "Nat": "SWE",
                },
                {
                    "IsTeam": False,
                    "Rank": "4",
                    "IBUId": "W4",
                    "Name": "Dana",
                    "Nat": "GER",
                },
            ],
        },
        "RREL": {
            "Competition": {"DisciplineId": "RL", "catId": "SW"},
            "Results": [
                {"IsTeam": True, "Rank": "1", "Nat": "NOR"},
                {"IsTeam": True, "Rank": "2", "Nat": "FRA"},
                {"IsTeam": True, "Rank": "3", "Nat": "GER"},
                {
                    "IsTeam": False,
                    "Rank": "1",
                    "IBUId": "W1",
                    "Name": "Alice",
                    "Nat": "NOR",
                },
                {
                    "IsTeam": False,
                    "Rank": "2",
                    "IBUId": "W2",
                    "Name": "Beth",
                    "Nat": "FRA",
                },
                {
                    "IsTeam": False,
                    "Rank": "3",
                    "IBUId": "W4",
                    "Name": "Dana",
                    "Nat": "GER",
                },
            ],
        },
        "RMIX": {
            "Competition": {"DisciplineId": "MR", "catId": "MX"},
            "Results": [
                {"IsTeam": True, "Rank": "1", "Nat": "SWE"},
                {"IsTeam": True, "Rank": "2", "Nat": "NOR"},
                {"IsTeam": True, "Rank": "3", "Nat": "FRA"},
                {
                    "IsTeam": False,
                    "Rank": "1",
                    "IBUId": "W3",
                    "Name": "Cara",
                    "Nat": "SWE",
                },
                {
                    "IsTeam": False,
                    "Rank": "1",
                    "IBUId": "M1",
                    "Name": "Erik",
                    "Nat": "SWE",
                },
                {
                    "IsTeam": False,
                    "Rank": "2",
                    "IBUId": "W1",
                    "Name": "Alice",
                    "Nat": "NOR",
                },
                {
                    "IsTeam": False,
                    "Rank": "2",
                    "IBUId": "M2",
                    "Name": "Finn",
                    "Nat": "NOR",
                },
                {
                    "IsTeam": False,
                    "Rank": "3",
                    "IBUId": "W2",
                    "Name": "Beth",
                    "Nat": "FRA",
                },
                {
                    "IsTeam": False,
                    "Rank": "3",
                    "IBUId": "M3",
                    "Name": "Gus",
                    "Nat": "FRA",
                },
            ],
        },
    }
    monkeypatch.setattr(
        achievements, "get_race_results", lambda race_id: payloads[race_id]
    )
    monkeypatch.setattr(
        achievements,
        "get_athlete_bio",
        lambda ibu_id: {"Gender": "W" if ibu_id.startswith("W") else "M"},
    )


def test_achievements_default_women_athlete_table(monkeypatch, capsys):
    _mock_world_cup_dataset(monkeypatch)

    rc = achievements.handle_achievements(_args())

    assert rc == 0
    out = capsys.readouterr().out
    assert "# Achievements" in out
    assert "World Cup" in out
    assert "1\tAlice\tNOR\tF\t-\t2\t1\t0\t3\t1\t0\t0\t1\t1\t1\t0\t2\t3\t1\t2\n" in out


def test_achievements_country_mode(monkeypatch, capsys):
    _mock_world_cup_dataset(monkeypatch)

    rc = achievements.handle_achievements(_args(country=True))

    assert rc == 0
    out = capsys.readouterr().out
    assert "Country" in out
    assert "1\tNOR\t2\t1\t0\t3\t1\t0\t0\t1\t1\t1\t0\t2\n" in out


def test_achievements_season_all_aggregates(monkeypatch, capsys):
    monkeypatch.setattr(
        achievements,
        "get_seasons",
        lambda: [
            {"SeasonId": "2425", "IsCurrent": False},
            {"SeasonId": "2526", "IsCurrent": True},
        ],
    )
    monkeypatch.setattr(achievements, "get_current_season_id", lambda: "2526")
    monkeypatch.setattr(
        achievements,
        "get_events",
        lambda season_id, level: [
            {
                "EventId": f"E{season_id}",
                "Description": f"BMW IBU World Cup {season_id}",
            }
        ],
    )
    monkeypatch.setattr(
        achievements,
        "get_races",
        lambda event_id: [
            {
                "RaceId": f"R{event_id}",
                "DisciplineId": "SP",
                "catId": "SW",
                "StartTime": "2026-01-01T10:00:00Z",
            }
        ],
    )

    payloads = {
        "RE2425": {
            "Competition": {"DisciplineId": "SP", "catId": "SW"},
            "Results": [
                {
                    "IsTeam": False,
                    "Rank": "1",
                    "IBUId": "W1",
                    "Name": "Alice",
                    "Nat": "NOR",
                },
                {
                    "IsTeam": False,
                    "Rank": "2",
                    "IBUId": "W2",
                    "Name": "Beth",
                    "Nat": "FRA",
                },
            ],
        },
        "RE2526": {
            "Competition": {"DisciplineId": "SP", "catId": "SW"},
            "Results": [
                {
                    "IsTeam": False,
                    "Rank": "1",
                    "IBUId": "W2",
                    "Name": "Beth",
                    "Nat": "FRA",
                },
                {
                    "IsTeam": False,
                    "Rank": "2",
                    "IBUId": "W1",
                    "Name": "Alice",
                    "Nat": "NOR",
                },
            ],
        },
    }
    monkeypatch.setattr(
        achievements, "get_race_results", lambda race_id: payloads[race_id]
    )
    monkeypatch.setattr(achievements, "get_athlete_bio", lambda ibu_id: {"Gender": "W"})

    rc = achievements.handle_achievements(_args(season="all"))

    assert rc == 0
    out = capsys.readouterr().out
    assert "all seasons" in out
    assert "\tG\tSP\tPU\tIN\tMS\n" in out
    assert (
        "\tAlice\tNOR\tF\t-\t1\t1\t0\t2\t1\t1\t0\t2\t0\t0\t0\t0\t2\t2\t0\t0\t0\t0\t0\t0\n"
        in out
    )
    assert (
        "\tBeth\tFRA\tF\t-\t1\t1\t0\t2\t1\t1\t0\t2\t0\t0\t0\t0\t2\t2\t0\t0\t0\t0\t0\t0\n"
        in out
    )


def test_achievements_no_races_returns_error(monkeypatch, capsys):
    monkeypatch.setattr(
        achievements, "get_seasons", lambda: [{"SeasonId": "2526", "IsCurrent": True}]
    )
    monkeypatch.setattr(achievements, "get_current_season_id", lambda: "2526")
    monkeypatch.setattr(achievements, "get_events", lambda season_id, level: [])

    rc = achievements.handle_achievements(_args())

    assert rc == 1
    err = capsys.readouterr().err
    assert "no races found for the requested scope" in err


def test_achievements_filters_by_nationality(monkeypatch, capsys):
    _mock_world_cup_dataset(monkeypatch)

    rc = achievements.handle_achievements(_args(nationality="fra"))

    assert rc == 0
    out = capsys.readouterr().out
    assert "# Nationality filter: FRA" in out
    assert "\tBeth\tFRA\tF\t-\t" in out
    assert "\tAlice\tNOR\tF\t-\t" not in out
    assert "\tCara\tSWE\tF\t-\t" not in out


def test_sort_stats_rows_uses_new_medal_priority_then_races_then_races_ind():
    rows = [
        {
            "name": "Alpha",
            "gold": 1,
            "gold_ind": 1,
            "silver": 0,
            "silver_ind": 0,
            "bronze": 0,
            "bronze_ind": 0,
            "races": 9,
            "races_ind": 4,
        },
        {
            "name": "Beta",
            "gold": 1,
            "gold_ind": 0,
            "silver": 5,
            "silver_ind": 5,
            "bronze": 0,
            "bronze_ind": 0,
            "races": 1,
            "races_ind": 1,
        },
        {
            "name": "Delta",
            "gold": 1,
            "gold_ind": 1,
            "silver": 0,
            "silver_ind": 0,
            "bronze": 0,
            "bronze_ind": 0,
            "races": 3,
            "races_ind": 1,
        },
        {
            "name": "Epsilon",
            "gold": 1,
            "gold_ind": 1,
            "silver": 0,
            "silver_ind": 0,
            "bronze": 0,
            "bronze_ind": 0,
            "races": 3,
            "races_ind": 2,
        },
        {
            "name": "Gamma",
            "gold": 2,
            "gold_ind": 0,
            "silver": 0,
            "silver_ind": 0,
            "bronze": 0,
            "bronze_ind": 0,
            "races": 20,
            "races_ind": 20,
        },
    ]

    sorted_rows = achievements._sort_stats_rows(rows, "name")

    assert [row["name"] for row in sorted_rows] == [
        "Gamma",
        "Delta",
        "Epsilon",
        "Alpha",
        "Beta",
    ]


def test_wc_titles_section_for_completed_season(monkeypatch, capsys):
    monkeypatch.setattr(
        achievements,
        "get_events",
        lambda season_id, level: [
            {
                "EventId": "E2425",
                "Description": "BMW IBU World Cup Oslo",
                "StartDate": "2025-03-01T10:00:00Z",
                "EndDate": "2025-03-20T10:00:00Z",
            }
        ],
    )
    monkeypatch.setattr(
        achievements,
        "get_races",
        lambda event_id: [
            {
                "RaceId": "R2425",
                "DisciplineId": "SP",
                "catId": "SW",
                "StartTime": "2025-03-05T10:00:00Z",
            }
        ],
    )
    monkeypatch.setattr(
        achievements,
        "get_race_results",
        lambda race_id: {
            "Competition": {"DisciplineId": "SP", "catId": "SW"},
            "Results": [
                {
                    "IsTeam": False,
                    "Rank": "1",
                    "IBUId": "A1",
                    "Name": "Alice",
                    "Nat": "NOR",
                },
                {
                    "IsTeam": False,
                    "Rank": "2",
                    "IBUId": "B1",
                    "Name": "Beth",
                    "Nat": "FRA",
                },
                {
                    "IsTeam": False,
                    "Rank": "3",
                    "IBUId": "C1",
                    "Name": "Cara",
                    "Nat": "SWE",
                },
            ],
        },
    )
    monkeypatch.setattr(
        achievements,
        "get_cups",
        lambda season_id: [
            {"CupId": "TS2425", "Level": 1, "CatId": "SW", "DisciplineId": "TS"},
            {"CupId": "SP2425", "Level": 1, "CatId": "SW", "DisciplineId": "SP"},
            {"CupId": "PU2425", "Level": 1, "CatId": "SW", "DisciplineId": "PU"},
            {"CupId": "IN2425", "Level": 1, "CatId": "SW", "DisciplineId": "IN"},
            {"CupId": "MS2425", "Level": 1, "CatId": "SW", "DisciplineId": "MS"},
        ],
    )
    monkeypatch.setattr(
        achievements,
        "get_cup_results",
        lambda cup_id: {
            "Rows": [
                {
                    "Rank": "1",
                    "IBUId": "A1" if cup_id in {"TS2425", "SP2425"} else "B1",
                    "Name": "Alice" if cup_id in {"TS2425", "SP2425"} else "Beth",
                    "Nat": "NOR" if cup_id in {"TS2425", "SP2425"} else "FRA",
                }
            ]
        },
    )
    monkeypatch.setattr(achievements, "get_athlete_bio", lambda ibu_id: {"Gender": "W"})

    rc = achievements.handle_achievements(_args(season="2425"))

    assert rc == 0
    out = capsys.readouterr().out
    assert "\tG\tSP\tPU\tIN\tMS\n" in out
    assert (
        "\tAlice\tNOR\tF\t-\t1\t0\t0\t1\t1\t0\t0\t1\t0\t0\t0\t0\t1\t1\t0\t1\t1\t0\t0\t0\n"
        in out
    )
    assert (
        "\tBeth\tFRA\tF\t-\t0\t1\t0\t1\t0\t1\t0\t1\t0\t0\t0\t0\t1\t1\t0\t0\t0\t1\t1\t1\n"
        in out
    )


def test_wc_titles_section_shown_with_season_all(monkeypatch, capsys):
    monkeypatch.setattr(
        achievements,
        "get_seasons",
        lambda: [
            {"SeasonId": "2425", "IsCurrent": False},
            {"SeasonId": "2526", "IsCurrent": True},
        ],
    )
    monkeypatch.setattr(achievements, "get_current_season_id", lambda: "2526")
    monkeypatch.setattr(
        achievements,
        "get_events",
        lambda season_id, level: [
            {
                "EventId": f"E{season_id}",
                "Description": f"BMW IBU World Cup {season_id}",
                "StartDate": "2025-03-01T10:00:00Z"
                if season_id == "2425"
                else "2026-02-01T10:00:00Z",
                "EndDate": "2025-03-20T10:00:00Z"
                if season_id == "2425"
                else "2026-03-20T10:00:00Z",
            }
        ],
    )
    monkeypatch.setattr(
        achievements,
        "get_races",
        lambda event_id: [
            {
                "RaceId": f"R{event_id}",
                "DisciplineId": "SP",
                "catId": "SW",
                "StartTime": "2025-03-05T10:00:00Z"
                if event_id == "E2425"
                else "2026-02-05T10:00:00Z",
            }
        ],
    )
    monkeypatch.setattr(
        achievements,
        "get_race_results",
        lambda race_id: {
            "Competition": {"DisciplineId": "SP", "catId": "SW"},
            "Results": [
                {
                    "IsTeam": False,
                    "Rank": "1",
                    "IBUId": "A1",
                    "Name": "Alice",
                    "Nat": "NOR",
                },
                {
                    "IsTeam": False,
                    "Rank": "2",
                    "IBUId": "B1",
                    "Name": "Beth",
                    "Nat": "FRA",
                },
                {
                    "IsTeam": False,
                    "Rank": "3",
                    "IBUId": "C1",
                    "Name": "Cara",
                    "Nat": "SWE",
                },
            ],
        },
    )
    monkeypatch.setattr(
        achievements,
        "get_cups",
        lambda season_id: [
            {"CupId": "TS2425", "Level": 1, "CatId": "SW", "DisciplineId": "TS"},
            {"CupId": "SP2425", "Level": 1, "CatId": "SW", "DisciplineId": "SP"},
            {"CupId": "PU2425", "Level": 1, "CatId": "SW", "DisciplineId": "PU"},
            {"CupId": "IN2425", "Level": 1, "CatId": "SW", "DisciplineId": "IN"},
            {"CupId": "MS2425", "Level": 1, "CatId": "SW", "DisciplineId": "MS"},
        ]
        if season_id == "2425"
        else [],
    )
    monkeypatch.setattr(
        achievements,
        "get_cup_results",
        lambda cup_id: {
            "Rows": [
                {
                    "Rank": "1",
                    "IBUId": "A1",
                    "Name": "Alice",
                    "Nat": "NOR",
                }
            ]
        },
    )
    monkeypatch.setattr(achievements, "get_athlete_bio", lambda ibu_id: {"Gender": "W"})

    rc = achievements.handle_achievements(_args(season="all"))

    assert rc == 0
    out = capsys.readouterr().out
    assert "\tG\tSP\tPU\tIN\tMS\n" in out
    assert (
        "\tAlice\tNOR\tF\t-\t2\t0\t0\t2\t2\t0\t0\t2\t0\t0\t0\t0\t2\t2\t0\t1\t1\t1\t1\t1\n"
        in out
    )


def test_achievements_pretty_marks_leaders_and_u23(monkeypatch):
    _mock_world_cup_dataset(monkeypatch)
    monkeypatch.setattr(
        achievements,
        "get_athlete_bio",
        lambda ibu_id: (
            {"BirthDate": "2004-01-01"}
            if ibu_id == "W1"
            else {"BirthDate": "1998-01-01"}
        ),
    )
    monkeypatch.setattr(achievements.Color, "enabled", classmethod(lambda cls: True))

    captured: dict = {}

    def fake_render_table(headers, rows, **kwargs):
        captured["headers"] = headers
        captured["rows"] = rows
        captured["kwargs"] = kwargs

    monkeypatch.setattr(achievements, "render_table", fake_render_table)

    rc = achievements.handle_achievements(_args(format=""))

    assert rc == 0
    assert captured["headers"][:5] == ["#", "Athlete", "Nat", "Gender", "Age"]
    assert "(U23)" in captured["rows"][0][4]
    assert achievements.GENERAL_LEADER_MARKER in captured["rows"][0][1]
    assert achievements.U23_LEADER_MARKER in captured["rows"][0][1]

    name_formatter = captured["kwargs"]["cell_formatters"][1]
    assert name_formatter is not None
    rendered = name_formatter(captured["rows"][0][1], 0)
    assert "●" in rendered
    assert achievements.GENERAL_LEADER_MARKER not in rendered
    assert achievements.U23_LEADER_MARKER not in rendered


def test_achievements_pretty_uses_standings_context_for_markers_and_u23(monkeypatch):
    _mock_world_cup_dataset(monkeypatch)
    monkeypatch.setattr(achievements.Color, "enabled", classmethod(lambda cls: True))
    monkeypatch.setattr(
        achievements,
        "_build_wc_standings_context",
        lambda season_id, category: {
            "age_display_by_id": {"W4": "22 (U23)"},
            "u23_ids": {"W4"},
            "best_u23_ids": set(),
            "markers_by_id": {
                "W1": [
                    achievements.GENERAL_LEADER_MARKER,
                    achievements.DISCIPLINE_LEADER_MARKER,
                    achievements.DISCIPLINE_LEADER_MARKER,
                    achievements.DISCIPLINE_LEADER_MARKER,
                ]
            },
            "markers_by_name_nat": {},
            "reference_date": None,
        },
    )

    captured: dict = {}

    def fake_render_table(headers, rows, **kwargs):
        captured["headers"] = headers
        captured["rows"] = rows
        captured["kwargs"] = kwargs

    monkeypatch.setattr(achievements, "render_table", fake_render_table)

    rc = achievements.handle_achievements(_args(format=""))

    assert rc == 0
    assert captured["headers"][:5] == ["#", "Athlete", "Nat", "Gender", "Age"]

    alice_row = next(row for row in captured["rows"] if row[1].startswith("Alice"))
    assert alice_row[1].count(achievements.GENERAL_LEADER_MARKER) == 1
    assert alice_row[1].count(achievements.DISCIPLINE_LEADER_MARKER) == 3

    dana_row = next(row for row in captured["rows"] if row[1].startswith("Dana"))
    assert dana_row[4] == "22 (U23)"


def test_achievements_pretty_falls_back_when_standings_markers_do_not_match_rows(
    monkeypatch,
):
    _mock_world_cup_dataset(monkeypatch)
    monkeypatch.setattr(
        achievements,
        "get_athlete_bio",
        lambda ibu_id: (
            {"BirthDate": "2004-01-01"}
            if ibu_id == "W1"
            else {"BirthDate": "1998-01-01"}
        ),
    )
    monkeypatch.setattr(achievements.Color, "enabled", classmethod(lambda cls: True))
    monkeypatch.setattr(
        achievements,
        "_build_wc_standings_context",
        lambda season_id, category: {
            "age_display_by_id": {},
            "u23_ids": set(),
            "best_u23_ids": set(),
            "markers_by_id": {"X1": [achievements.GENERAL_LEADER_MARKER]},
            "markers_by_name_nat": {
                ("Unknown Athlete", "XXX"): [achievements.GENERAL_LEADER_MARKER]
            },
            "reference_date": None,
        },
    )

    captured: dict = {}

    def fake_render_table(headers, rows, **kwargs):
        captured["headers"] = headers
        captured["rows"] = rows
        captured["kwargs"] = kwargs

    monkeypatch.setattr(achievements, "render_table", fake_render_table)

    rc = achievements.handle_achievements(_args(format=""))

    assert rc == 0
    assert achievements.GENERAL_LEADER_MARKER in captured["rows"][0][1]
    assert achievements.U23_LEADER_MARKER in captured["rows"][0][1]
