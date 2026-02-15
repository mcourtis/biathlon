"""Tests for startlist pre-race snapshot helpers."""

import datetime

from biathlon.api import BiathlonError
from biathlon.commands import startlist


def _dt(value: str) -> datetime.datetime:
    return datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))


def test_filter_results_before_cutoff_excludes_target_and_future(monkeypatch):
    cutoff = _dt("2026-01-10T10:00:00Z")
    start_by_race = {
        "RACE_PAST": "2026-01-02T10:00:00Z",
        "RACE_FUTURE": "2026-01-14T10:00:00Z",
    }

    def fake_get_race_results(race_id: str) -> dict:
        if race_id not in start_by_race:
            raise BiathlonError("missing")
        return {"Competition": {"StartTime": start_by_race[race_id]}}

    monkeypatch.setattr(startlist, "get_race_results", fake_get_race_results)

    rows = [
        {"RaceId": "RACE_TARGET"},
        {"RaceId": "RACE_PAST"},
        {"RaceId": "RACE_FUTURE"},
        {"RaceId": "RACE_UNKNOWN"},
    ]
    cache = {"RACE_TARGET": cutoff}

    filtered = startlist._filter_results_before_cutoff(
        rows,
        "RACE_TARGET",
        cutoff,
        cache,
    )

    assert [row["RaceId"] for row in filtered] == ["RACE_PAST"]


def test_compute_wc_pre_race_standings_uses_strict_cutoff(monkeypatch):
    races = [
        (_dt("2026-01-01T10:00:00Z"), "RACE_OLD", "SP"),
        (_dt("2026-01-10T10:00:00Z"), "RACE_TARGET", "SP"),
        (_dt("2026-01-20T10:00:00Z"), "RACE_NEW", "SP"),
    ]
    monkeypatch.setattr(
        startlist,
        "_collect_wc_individual_races",
        lambda season_id, cat_id: races,
    )

    payloads = {
        "RACE_OLD": {
            "Results": [
                {
                    "IsTeam": False,
                    "Rank": "1",
                    "IBUId": "A",
                    "Name": "Alpha",
                    "Nat": "NOR",
                },
                {
                    "IsTeam": False,
                    "Rank": "2",
                    "IBUId": "B",
                    "Name": "Beta",
                    "Nat": "SWE",
                },
            ]
        },
        "RACE_TARGET": {
            "Results": [
                {
                    "IsTeam": False,
                    "Rank": "1",
                    "IBUId": "B",
                    "Name": "Beta",
                    "Nat": "SWE",
                }
            ]
        },
        "RACE_NEW": {
            "Results": [
                {
                    "IsTeam": False,
                    "Rank": "1",
                    "IBUId": "C",
                    "Name": "Gamma",
                    "Nat": "FRA",
                }
            ]
        },
    }
    monkeypatch.setattr(
        startlist, "get_race_results", lambda race_id: payloads[race_id]
    )

    total_rows, disc_rows = startlist._compute_wc_pre_race_standings(
        "2526",
        "SW",
        "RACE_TARGET",
        "SP",
        _dt("2026-01-10T10:00:00Z"),
    )

    assert [row["IBUId"] for row in total_rows] == ["A", "B"]
    assert total_rows[0]["Score"] == 90
    assert total_rows[1]["Score"] == 75
    assert [row["IBUId"] for row in disc_rows] == ["A", "B"]


def test_find_all_startlist_races_keeps_startlist_only(monkeypatch):
    monkeypatch.setattr(startlist, "get_current_season_id", lambda: "2526")
    monkeypatch.setattr(
        startlist,
        "get_events",
        lambda season_id, level: [
            {"EventId": "E1", "EndDate": "2999-01-01"},
        ],
    )
    monkeypatch.setattr(
        startlist,
        "get_races",
        lambda event_id: [
            {"RaceId": "RACE_A"},
            {"RaceId": "RACE_B"},
        ],
    )

    def fake_get_race_results(race_id: str) -> dict:
        if race_id == "RACE_A":
            return {
                "IsStartList": False,
                "Competition": {"StartTime": "2026-01-10T10:00:00Z"},
            }
        return {
            "IsStartList": True,
            "Competition": {"StartTime": "2026-01-11T10:00:00Z"},
        }

    monkeypatch.setattr(startlist, "get_race_results", fake_get_race_results)

    races = startlist._find_all_startlist_races()

    assert [race_id for race_id, _payload in races] == ["RACE_B"]
