"""Tests for brief startlist handler snapshot behavior."""

import argparse
import datetime
import re

from biathlon.commands import brief


def _dt(value: str) -> datetime.datetime:
    return datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))


def test_handle_brief_startlist_completed_race_uses_snapshot(monkeypatch):
    payload = {
        "IsStartList": False,
        "Competition": {
            "DisciplineId": "SP",
            "catId": "SW",
            "StartTime": "2026-01-10T10:00:00Z",
        },
        "SportEvt": {"SeasonId": "2526"},
        "Results": [
            {
                "IsTeam": False,
                "IBUId": "A",
                "Name": "Alpha",
                "Nat": "NOR",
                "Rank": "1",
                "Result": "22:00.0",
            }
        ],
    }
    monkeypatch.setattr(brief, "get_race_results", lambda race_id: payload)
    monkeypatch.setattr(brief, "format_race_header", lambda payload, race_id: "Header")

    captured: dict = {}

    def fake_prepare(payload, race_id, args, **kwargs):
        captured.update(kwargs)
        return {"entries": [{"ibu_id": "A", "name": "Alpha", "nat": "NOR"}]}

    monkeypatch.setattr(brief, "_prepare_startlist_context", fake_prepare)
    monkeypatch.setattr(brief, "render_startlist_analysis", lambda ctx, args: None)

    args = argparse.Namespace(race="RACE1", major=False, format="tsv")
    rc = brief.handle_brief_startlist(args)

    assert rc == 0
    assert captured["snapshot_target_race_id"] == "RACE1"
    assert captured["snapshot_cutoff_dt"] == _dt("2026-01-10T10:00:00Z")
    assert args.leader_markers is True


def test_handle_brief_startlist_completed_race_requires_start_datetime(
    monkeypatch, capsys
):
    payload = {
        "IsStartList": False,
        "Competition": {"DisciplineId": "SP", "catId": "SW"},
        "SportEvt": {"SeasonId": "2526"},
        "Results": [
            {
                "IsTeam": False,
                "IBUId": "A",
                "Name": "Alpha",
                "Nat": "NOR",
                "Rank": "1",
                "Result": "22:00.0",
            }
        ],
    }
    monkeypatch.setattr(brief, "get_race_results", lambda race_id: payload)

    args = argparse.Namespace(race="RACE1", major=False, format="tsv")
    rc = brief.handle_brief_startlist(args)

    assert rc == 1
    assert (
        "does not expose a race start datetime for snapshot mode"
        in capsys.readouterr().err
    )


def test_handle_brief_startlist_keeps_startlist_mode_for_is_startlist_true(monkeypatch):
    payload = {
        "IsStartList": True,
        "Competition": {
            "DisciplineId": "SP",
            "catId": "SW",
            "StartTime": "2026-01-10T10:00:00Z",
        },
        "SportEvt": {"SeasonId": "2526"},
        "Results": [
            {
                "IsTeam": False,
                "IBUId": "A",
                "Name": "Alpha",
                "Nat": "NOR",
            }
        ],
    }
    monkeypatch.setattr(brief, "get_race_results", lambda race_id: payload)
    monkeypatch.setattr(brief, "format_race_header", lambda payload, race_id: "Header")

    captured: dict = {}

    def fake_prepare(payload, race_id, args, **kwargs):
        captured.update(kwargs)
        return {"entries": [{"ibu_id": "A", "name": "Alpha", "nat": "NOR"}]}

    monkeypatch.setattr(brief, "_prepare_startlist_context", fake_prepare)
    monkeypatch.setattr(brief, "render_startlist_analysis", lambda ctx, args: None)

    args = argparse.Namespace(race="RACE1", major=False, format="tsv")
    rc = brief.handle_brief_startlist(args)

    assert rc == 0
    assert captured["snapshot_target_race_id"] == ""
    assert captured["snapshot_cutoff_dt"] is None


def test_render_team_startlist_section6_includes_startlist_olympic_medalists(
    monkeypatch, capsys
):
    def athlete_stats(name: str, nat: str, gold: int, silver: int, bronze: int) -> dict:
        return {
            "name": name,
            "nat": nat,
            "gender": "M",
            "gold": gold,
            "silver": silver,
            "bronze": bronze,
            "races": gold + silver + bronze,
            "gold_ind": 0,
            "silver_ind": 0,
            "bronze_ind": 0,
            "races_ind": 0,
            "gold_relay": gold,
            "silver_relay": silver,
            "bronze_relay": bronze,
            "races_relay": gold + silver + bronze,
        }

    payload = {
        "Competition": {"DisciplineId": "RL", "catId": "SM"},
        "SportEvt": {"Description": "Olympic Winter Games"},
        "Results": [
            {
                "IsTeam": False,
                "IBUId": "S1",
                "FamilyName": "Startlist",
                "Name": "Silver Startlist",
                "Nat": "NOR",
            },
            {
                "IsTeam": False,
                "IBUId": "N1",
                "FamilyName": "NoMedal",
                "Name": "No Medal",
                "Nat": "SWE",
            },
        ],
    }
    team_entries = [{"bib": "1", "name": "Norway", "nat": "NOR"}]

    monkeypatch.setattr(
        brief, "detect_event_type", lambda _sport_evt: brief.EVENT_TYPE_OWG
    )
    monkeypatch.setattr(brief, "_get_past_olympic_relay_podiums", lambda *_a, **_k: [])
    monkeypatch.setattr(
        brief,
        "_get_all_olympic_medals",
        lambda *_a, **_k: (
            [],
            {
                "G1": athlete_stats("Gold Winner", "FRA", 1, 0, 0),
                "S1": athlete_stats("Silver Startlist", "NOR", 0, 1, 0),
                "X1": athlete_stats("Other Silver", "GER", 0, 2, 0),
            },
        ),
    )

    args = argparse.Namespace(format="tsv")
    rc = brief._render_team_startlist(payload, "RACE1", team_entries, args)

    assert rc == 0
    out = capsys.readouterr().out
    assert "Athlete medal table (all Olympic disciplines)" in out
    assert "3\tSilver Startlist\tNOR\tM\t0\t1\t0\t1\t1" in out
    assert "Other Silver\tGER\tM" not in out


def test_render_team_startlist_section5_includes_startlist_discipline_medalists(
    monkeypatch, capsys
):
    payload = {
        "Competition": {"DisciplineId": "RL", "catId": "SM"},
        "SportEvt": {"Description": "Olympic Winter Games"},
        "Results": [
            {
                "IsTeam": False,
                "IBUId": "S1",
                "FamilyName": "Startlist",
                "Name": "Silver Startlist",
                "Nat": "NOR",
            }
        ],
    }
    team_entries = [{"bib": "1", "name": "Norway", "nat": "NOR"}]
    podiums = [
        {
            "year": "2026",
            "venue": "Antholz",
            "country": "Italy",
            "gold": "France (FRA)",
            "silver": "Germany (GER)",
            "bronze": "Norway (NOR)",
            "gold_athletes": [
                {
                    "name": "Gold",
                    "full_name": "Gold Winner",
                    "nat": "FRA",
                    "gender": "M",
                }
            ],
            "silver_athletes": [
                {
                    "name": "Other",
                    "full_name": "Other Silver",
                    "nat": "GER",
                    "gender": "M",
                }
            ],
            "bronze_athletes": [
                {
                    "name": "Bronze",
                    "full_name": "Bronze Guy",
                    "nat": "NOR",
                    "gender": "M",
                }
            ],
        },
        {
            "year": "2022",
            "venue": "Beijing",
            "country": "China",
            "gold": "France (FRA)",
            "silver": "Norway (NOR)",
            "bronze": "Sweden (SWE)",
            "gold_athletes": [
                {
                    "name": "Gold",
                    "full_name": "Gold Winner",
                    "nat": "FRA",
                    "gender": "M",
                }
            ],
            "silver_athletes": [
                {
                    "name": "Startlist",
                    "full_name": "Silver Startlist",
                    "nat": "NOR",
                    "gender": "M",
                }
            ],
            "bronze_athletes": [],
        },
    ]

    monkeypatch.setattr(
        brief, "detect_event_type", lambda _sport_evt: brief.EVENT_TYPE_OWG
    )
    monkeypatch.setattr(
        brief, "_get_past_olympic_relay_podiums", lambda *_a, **_k: podiums
    )
    monkeypatch.setattr(brief, "_get_all_olympic_medals", lambda *_a, **_k: ([], {}))

    args = argparse.Namespace(format="tsv")
    rc = brief._render_team_startlist(payload, "RACE1", team_entries, args)

    assert rc == 0
    out = capsys.readouterr().out
    assert "Athlete medal table (Men Relay)" in out
    assert "3\tSilver Startlist\tNOR\tM\t0\t1\t0\t1\t1" in out
    assert "Other Silver\tGER\tM\t0\t1\t0\t1\t1" not in out


def test_render_team_startlist_section2a_current_season_podiums(monkeypatch, capsys):
    payload = {
        "Competition": {"DisciplineId": "RL", "catId": "SM"},
        "SportEvt": {"Description": "Olympic Winter Games", "SeasonId": "2526"},
        "Results": [
            {
                "IsTeam": False,
                "IBUId": "A1",
                "FamilyName": "Laegreid",
                "Name": "Sturla Holm Laegreid",
                "Nat": "NOR",
            }
        ],
    }
    team_entries = [{"bib": "1", "name": "Norway", "nat": "NOR"}]

    monkeypatch.setattr(
        brief, "detect_event_type", lambda _sport_evt: brief.EVENT_TYPE_OWG
    )
    monkeypatch.setattr(brief, "_get_past_olympic_relay_podiums", lambda *_a, **_k: [])
    monkeypatch.setattr(
        brief,
        "_get_current_season_relay_podiums",
        lambda *_a, **_k: [
            {
                "date": "2025-12-14",
                "race_type": "World Cup",
                "year": "2025",
                "season": "2526",
                "venue": "Kontiolahti",
                "country": "Finland",
                "gold": "NORWAY",
                "silver": "FRANCE",
                "bronze": "GERMANY",
                "gold_athletes": [{"name": "Laegreid"}],
                "silver_athletes": [{"name": "Claude"}],
                "bronze_athletes": [{"name": "Lesser"}],
            }
        ],
    )
    monkeypatch.setattr(
        brief, "_fetch_relay_wc_standings", lambda *_a, **_k: ("Men Relay", [])
    )
    monkeypatch.setattr(brief, "_get_all_olympic_medals", lambda *_a, **_k: ([], {}))

    args = argparse.Namespace(format="tsv")
    rc = brief._render_team_startlist(payload, "RACE1", team_entries, args)

    assert rc == 0
    out = capsys.readouterr().out
    assert "Previous Men Relay podiums" in out
    assert "Previous Men Relay podiums\n\nDate\tType\tVenue\tCountry" in out
    assert (
        "2025-12-14\tWorld Cup\tKontiolahti\tFinland\tNORWAY\tLaegreid\tFRANCE\tClaude\tGERMANY\tLesser"
        in out
    )


def test_render_team_startlist_section2a_season_separator_pretty(monkeypatch, capsys):
    payload = {
        "Competition": {"DisciplineId": "RL", "catId": "SM"},
        "SportEvt": {"Description": "Olympic Winter Games", "SeasonId": "2526"},
        "Results": [
            {
                "IsTeam": False,
                "IBUId": "A1",
                "FamilyName": "Laegreid",
                "Name": "Sturla Holm Laegreid",
                "Nat": "NOR",
            }
        ],
    }
    team_entries = [{"bib": "1", "name": "Norway", "nat": "NOR"}]

    monkeypatch.setattr(
        brief, "detect_event_type", lambda _sport_evt: brief.EVENT_TYPE_OWG
    )
    monkeypatch.setattr(brief, "_get_past_olympic_relay_podiums", lambda *_a, **_k: [])
    monkeypatch.setattr(
        brief,
        "_get_current_season_relay_podiums",
        lambda *_a, **_k: [
            {
                "date": "2025-12-14",
                "race_type": "World Cup",
                "year": "2025",
                "season": "2526",
                "venue": "Kontiolahti",
                "country": "Finland",
                "gold": "NORWAY",
                "silver": "FRANCE",
                "bronze": "GERMANY",
                "gold_athletes": [{"name": "Laegreid"}],
                "silver_athletes": [{"name": "Claude"}],
                "bronze_athletes": [{"name": "Lesser"}],
            },
            {
                "date": "2025-03-01",
                "race_type": "World Championship",
                "year": "2025",
                "season": "2425",
                "venue": "Oslo",
                "country": "Norway",
                "gold": "NORWAY",
                "silver": "GERMANY",
                "bronze": "FRANCE",
                "gold_athletes": [{"name": "Laegreid"}],
                "silver_athletes": [{"name": "Lesser"}],
                "bronze_athletes": [{"name": "Claude"}],
            },
            {
                "date": "2024-12-08",
                "race_type": "World Championship",
                "year": "2024",
                "season": "2425",
                "venue": "Hochfilzen",
                "country": "Austria",
                "gold": "FRANCE",
                "silver": "NORWAY",
                "bronze": "GERMANY",
                "gold_athletes": [{"name": "Claude"}],
                "silver_athletes": [{"name": "Laegreid"}],
                "bronze_athletes": [{"name": "Lesser"}],
            },
        ],
    )
    monkeypatch.setattr(
        brief, "_fetch_relay_wc_standings", lambda *_a, **_k: ("Men Relay", [])
    )
    monkeypatch.setattr(brief, "_get_all_olympic_medals", lambda *_a, **_k: ([], {}))

    args = argparse.Namespace(format="pretty")
    rc = brief._render_team_startlist(payload, "RACE1", team_entries, args)

    assert rc == 0
    out = capsys.readouterr().out
    clean = re.sub(r"\x1b\[[0-9;]*m", "", out)
    start = clean.index("Previous Men Relay podiums")
    block = clean[start:]
    lines = [line for line in block.splitlines() if line.strip()]

    idx_2025_dec = next(i for i, line in enumerate(lines) if "2025-12-14" in line)
    idx_2025_mar = next(i for i, line in enumerate(lines) if "2025-03-01" in line)
    idx_2024_dec = next(i for i, line in enumerate(lines) if "2024-12-08" in line)
    # Separator appears at season boundary (2526 -> 2425).
    assert idx_2025_mar == idx_2025_dec + 2
    assert "-+-" in lines[idx_2025_dec + 1]
    # No separator within the same season even if calendar year changes.
    assert idx_2024_dec == idx_2025_mar + 1


def test_render_team_startlist_adds_relay_wc_standings_section(monkeypatch, capsys):
    payload = {
        "Competition": {"DisciplineId": "RL", "catId": "SM"},
        "SportEvt": {"Description": "Olympic Winter Games", "SeasonId": "2526"},
        "Results": [
            {
                "IsTeam": False,
                "IBUId": "A1",
                "FamilyName": "Laegreid",
                "Name": "Sturla Holm Laegreid",
                "Nat": "NOR",
            }
        ],
    }
    team_entries = [
        {"bib": "1", "name": "Norway", "nat": "NOR"},
        {"bib": "2", "name": "France", "nat": "FRA"},
    ]

    monkeypatch.setattr(
        brief, "detect_event_type", lambda _sport_evt: brief.EVENT_TYPE_OWG
    )
    monkeypatch.setattr(brief, "_get_past_olympic_relay_podiums", lambda *_a, **_k: [])
    monkeypatch.setattr(
        brief, "_get_current_season_relay_podiums", lambda *_a, **_k: []
    )
    monkeypatch.setattr(brief, "_get_all_olympic_medals", lambda *_a, **_k: ([], {}))
    monkeypatch.setattr(
        brief,
        "_fetch_relay_wc_standings",
        lambda *_a, **_k: (
            "Men Relay",
            [
                {"Rank": "1", "Name": "NORWAY", "Nat": "NOR", "Score": "220"},
                {"Standing": "2", "Name": "FRANCE", "Nat": "FRA", "Points": "198"},
            ],
        ),
    )

    args = argparse.Namespace(format="tsv")
    rc = brief._render_team_startlist(payload, "RACE1", team_entries, args)

    assert rc == 0
    out = capsys.readouterr().out
    assert "Men Relay World Cup Standings (Top 10)" in out
    assert "1\tNORWAY\tNOR\t220" in out
    assert "2\tFRANCE\tFRA\t198" in out


def test_get_current_season_relay_podiums_includes_previous_and_country_fallback(
    monkeypatch,
):
    def fake_events(season_id: str, level: int = 1):
        assert level == 1
        if season_id == "2526":
            # No country fields: fallback should come from _event_country_display.
            return [{"EventId": "E_CUR", "Description": "BMW IBU World Cup"}]
        if season_id == "2425":
            return [
                {
                    "EventId": "E_PRE",
                    "CountryId": "ITA",
                    "Description": "BMW IBU World Championships",
                }
            ]
        return []

    def fake_races(event_id: str):
        if event_id == "E_CUR":
            return [
                {
                    "RaceId": "R_CUR",
                    "DisciplineId": "RL",
                    "catId": "SM",
                    "StartTime": "2025-12-14T11:00:00Z",
                },
                {
                    "RaceId": "RACE_TARGET",
                    "DisciplineId": "RL",
                    "catId": "SM",
                    "StartTime": "2026-01-01T11:00:00Z",
                },
            ]
        if event_id == "E_PRE":
            return [
                {
                    "RaceId": "R_PRE",
                    "DisciplineId": "RL",
                    "catId": "SM",
                    "StartTime": "2025-03-01T11:00:00Z",
                }
            ]
        return []

    def relay_payload(
        venue: str,
        start_time: str,
        bronze_name: str = "GERMANY",
        bronze_nat: str = "GER",
    ) -> dict:
        return {
            "IsResult": True,
            "Competition": {"StartTime": start_time},
            "SportEvt": {"Organizer": venue},
            "Results": [
                {"IsTeam": True, "Rank": "1", "Name": "NORWAY", "Nat": "NOR"},
                {"IsTeam": True, "Rank": "2", "Name": "FRANCE", "Nat": "FRA"},
                {"IsTeam": True, "Rank": "3", "Name": bronze_name, "Nat": bronze_nat},
                {
                    "IsTeam": False,
                    "Nat": "NOR",
                    "Leg": 1,
                    "FamilyName": "Laegreid",
                    "Name": "Sturla Holm Laegreid",
                },
                {
                    "IsTeam": False,
                    "Nat": "FRA",
                    "Leg": 1,
                    "FamilyName": "Claude",
                    "Name": "Fabien Claude",
                },
                {
                    "IsTeam": False,
                    "Nat": bronze_nat,
                    "Leg": 1,
                    "FamilyName": "Lesser",
                    "Name": "Erik Lesser",
                },
            ],
        }

    def fake_results(race_id: str) -> dict:
        if race_id == "R_CUR":
            return relay_payload("Kontiolahti", "2025-12-14T11:00:00Z")
        if race_id == "R_PRE":
            return relay_payload(
                "Oslo",
                "2025-03-01T11:00:00Z",
                bronze_name="SUI",
                bronze_nat="SUI",
            )
        raise AssertionError(f"Unexpected race_id: {race_id}")

    monkeypatch.setattr(brief, "get_events", fake_events)
    monkeypatch.setattr(brief, "get_races", fake_races)
    monkeypatch.setattr(brief, "get_race_results", fake_results)
    monkeypatch.setattr(
        brief,
        "_event_country_display",
        lambda event_id, _season_id: "Finland" if event_id == "E_CUR" else "",
    )

    rows = brief._get_current_season_relay_podiums(
        race_id="RACE_TARGET",
        season_id="2526",
        discipline="RL",
        category="SM",
    )

    assert len(rows) == 2
    assert [row["venue"] for row in rows] == ["Kontiolahti", "Oslo"]
    assert [row["date"] for row in rows] == ["2025-12-14", "2025-03-01"]
    assert [row["season"] for row in rows] == ["2526", "2425"]
    assert [row["race_type"] for row in rows] == ["World Cup", "World Championship"]
    assert rows[0]["country"] == "Finland"
    assert rows[1]["country"] == "Italy"
    assert rows[1]["bronze"] == "Switzerland"
