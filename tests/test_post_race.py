"""Tests for post-race deterministic snapshot helpers."""

import datetime

from biathlon.api import BiathlonError
from biathlon.commands import post_race


def _dt(value: str) -> datetime.datetime:
    return datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))


def test_filter_results_to_snapshot_excludes_future_and_warns_unknown(
    monkeypatch, capsys
):
    target_race_id = "RACE_TARGET"
    target_start = _dt("2026-01-10T10:00:00Z")

    start_by_race = {
        "RACE_PAST": "2026-01-01T10:00:00Z",
        "RACE_FUTURE": "2026-02-01T10:00:00Z",
    }

    def fake_get_race_results(race_id: str) -> dict:
        if race_id not in start_by_race:
            raise BiathlonError("missing")
        return {"Competition": {"StartTime": start_by_race[race_id]}}

    monkeypatch.setattr(post_race, "get_race_results", fake_get_race_results)

    rows = [
        {"RaceId": "RACE_TARGET"},
        {"RaceId": "RACE_PAST"},
        {"RaceId": "RACE_FUTURE"},
        {"RaceId": "RACE_UNKNOWN"},
    ]
    warning_keys: set[str] = set()
    race_start_cache = {target_race_id: target_start}

    filtered = post_race._filter_results_to_snapshot(
        rows,
        target_race_id,
        target_start,
        race_start_cache,
        warning_keys,
        "test context",
    )

    assert [row["RaceId"] for row in filtered] == ["RACE_TARGET", "RACE_PAST"]
    err = capsys.readouterr().err
    assert "warning: skipping row with unknown chronology" in err
    assert "RACE_UNKNOWN" in err


def test_is_result_at_or_before_target_uses_season_fast_path(monkeypatch):
    def fail_if_called(race_id: str) -> dict:
        raise AssertionError(f"unexpected API call for {race_id}")

    monkeypatch.setattr(post_race, "get_race_results", fail_if_called)

    warning_keys: set[str] = set()
    cache = {}
    target_race = "BT2526SWRLOG__SMPU"
    target_start = _dt("2026-02-15T10:15:00Z")

    past_row = {"RaceId": "BT2122SWRLOG__SMIN", "Season": "21/22"}
    future_row = {"RaceId": "BT2728SWRLCP01SMSP", "Season": "27/28"}

    assert (
        post_race._is_result_at_or_before_target(
            past_row,
            target_race,
            target_start,
            cache,
            warning_keys,
            "season fast path",
        )
        is True
    )
    assert (
        post_race._is_result_at_or_before_target(
            future_row,
            target_race,
            target_start,
            cache,
            warning_keys,
            "season fast path",
        )
        is False
    )


def test_is_lapped_current_result_detects_lap_by_irm():
    entry = {"irm": "LAP", "time": "+1:20.5"}
    assert post_race._is_lapped_current_result(entry, 10059, "PU") is True


def test_is_lapped_current_result_detects_pursuit_fallback_rank():
    entry = {"irm": "", "time": "39:10.0"}
    assert post_race._is_lapped_current_result(entry, 10060, "PU") is True
    assert post_race._is_lapped_current_result(entry, 10060, "SP") is False


def test_collect_discipline_race_ids_applies_cutoff_and_warns_unknown(
    monkeypatch, capsys
):
    monkeypatch.setattr(
        post_race, "get_events", lambda season_id, level: [{"EventId": "E"}]
    )
    monkeypatch.setattr(post_race, "detect_event_type", lambda event: "WC")
    monkeypatch.setattr(
        post_race,
        "get_races",
        lambda event_id: [
            {
                "RaceId": "RACE_OLD",
                "DisciplineId": "SP",
                "CatId": "SW",
                "StartTime": "2026-01-01T10:00:00Z",
            },
            {
                "RaceId": "RACE_NEW",
                "DisciplineId": "SP",
                "CatId": "SW",
                "StartTime": "2026-03-01T10:00:00Z",
            },
            {
                "RaceId": "RACE_UNKNOWN",
                "DisciplineId": "SP",
                "CatId": "SW",
            },
        ],
    )

    warning_keys: set[str] = set()
    race_ids = post_race._collect_discipline_race_ids(
        ["2526"],
        "SP",
        "SW",
        "WC",
        cutoff_dt=_dt("2026-02-01T00:00:00Z"),
        warning_keys=warning_keys,
        warning_context="medal races",
    )

    assert race_ids == ["RACE_OLD"]
    err = capsys.readouterr().err
    assert "warning: skipping race with unknown chronology in medal races" in err
    assert "RACE_UNKNOWN" in err


def test_has_newer_relevant_wc_points_race_detects_completed_newer(monkeypatch):
    races = [
        (_dt("2026-01-01T10:00:00Z"), "RACE_OLD", "SP"),
        (_dt("2026-01-10T10:00:00Z"), "RACE_TARGET", "SP"),
        (_dt("2026-01-20T10:00:00Z"), "RACE_NEW", "PU"),
    ]
    monkeypatch.setattr(
        post_race, "_collect_wc_individual_races", lambda season_id, cat_id: races
    )
    monkeypatch.setattr(
        post_race,
        "get_race_results",
        lambda race_id: {
            "Results": [
                {"IsTeam": False, "Rank": "1", "Result": "24:00.0", "IBUId": "A"}
            ]
        },
    )

    assert (
        post_race._has_newer_relevant_wc_points_race(
            "2526", "SW", "RACE_TARGET", _dt("2026-01-10T10:00:00Z")
        )
        is True
    )


def test_compute_wc_snapshot_rows_uses_target_cutoff(monkeypatch):
    races = [
        (_dt("2026-01-01T10:00:00Z"), "RACE_OLD", "SP"),
        (_dt("2026-01-10T10:00:00Z"), "RACE_TARGET", "SP"),
        (_dt("2026-01-20T10:00:00Z"), "RACE_NEW", "SP"),
    ]
    monkeypatch.setattr(
        post_race, "_collect_wc_individual_races", lambda season_id, cat_id: races
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
                },
                {
                    "IsTeam": False,
                    "Rank": "3",
                    "IBUId": "A",
                    "Name": "Alpha",
                    "Nat": "NOR",
                },
            ]
        },
        "RACE_NEW": {
            "Results": [
                {
                    "IsTeam": False,
                    "Rank": "1",
                    "IBUId": "A",
                    "Name": "Alpha",
                    "Nat": "NOR",
                },
            ]
        },
    }
    monkeypatch.setattr(
        post_race, "get_race_results", lambda race_id: payloads[race_id]
    )

    total_rows, disc_rows = post_race._compute_wc_snapshot_rows(
        "2526",
        "SW",
        "RACE_TARGET",
        "SP",
        _dt("2026-01-10T10:00:00Z"),
        set(),
    )

    assert total_rows[0]["IBUId"] == "B"
    assert total_rows[0]["Score"] == 165
    assert total_rows[0]["RnkDiff"] == -1
    assert disc_rows[0]["IBUId"] == "B"
