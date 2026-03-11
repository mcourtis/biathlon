"""Tests for brief startlist handler snapshot behavior."""

import argparse
import datetime

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


def test_handle_brief_startlist_prints_blank_line_after_main_header(
    monkeypatch, capsys
):
    payload = {
        "IsStartList": True,
        "Competition": {
            "DisciplineId": "SR",
            "catId": "MX",
            "StartTime": "2026-03-15T11:35:00Z",
        },
        "SportEvt": {"SeasonId": "2526"},
        "Results": [{"IsTeam": True, "Bib": "1", "Nat": "NOR"}],
    }
    monkeypatch.setattr(brief, "get_race_results", lambda race_id: payload)
    monkeypatch.setattr(
        brief,
        "format_race_header",
        lambda payload, race_id: "# Single Mixed Relay - Otepaa",
    )
    monkeypatch.setattr(
        brief,
        "_prepare_startlist_context",
        lambda *_a, **_k: {"entries": []},
    )
    monkeypatch.setattr(brief, "_build_startlist_entries", lambda payload: [])
    monkeypatch.setattr(
        brief,
        "_build_team_entries",
        lambda payload: [{"bib": "1", "name": "Norway", "nat": "NOR"}],
    )
    monkeypatch.setattr(brief, "render_startlist_analysis", lambda ctx, args: None)

    args = argparse.Namespace(race="RACE1", major=False, format="markdown")
    rc = brief.handle_brief_startlist(args)

    lines = capsys.readouterr().out.splitlines()

    assert rc == 0
    assert lines[:4] == [
        "",
        "# Single Mixed Relay - Otepaa",
        "",
        "Startlist entries: 0",
    ]


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
