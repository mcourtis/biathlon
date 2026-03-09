"""Tests for form command handler."""

import argparse
import types

from biathlon.commands import form


def _args(**overrides) -> argparse.Namespace:
    base = {
        "men": False,
        "startlist": None,
        "races": 5,
        "event": 0,
        "limit": 25,
        "top": 0,
        "nat": "",
        "min_pct": 0,
        "season": False,
        "remove": [],
        "include_relay": "",
        "format": "tsv",
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def _form_data(results: list[dict]) -> form.FormData:
    race_id = "R1"
    payload = {
        "Competition": {"DisciplineId": "SP", "catId": "SW"},
        "Results": results,
        "IsResult": True,
    }
    return form.FormData(
        season_id="2526",
        completed_race_ids=[race_id],
        season_race_ids=[race_id],
        race_payloads={race_id: payload},
        race_to_event={race_id: "E1"},
        race_is_relay={race_id: False},
        race_discipline={race_id: "SP"},
        race_category={race_id: "SW"},
        race_headers=["SP-Nov"],
        gender_cat="SW",
        gender_ibu_ids={"A1", "B1", "C1"},
        individual_race_ids=[race_id],
        relay_race_ids=[],
        race_course_times={},
        relay_leg_course_times={},
        all_candidate_ids=[race_id],
    )


def _athlete_result(ibu_id: str, name: str, nat: str, rank: str) -> dict:
    return {
        "IBUId": ibu_id,
        "Name": name,
        "Nat": nat,
        "Rank": rank,
        "Result": "30:00.0",
    }


def test_parse_nat_filter_normalizes_codes():
    parsed = form._parse_nat_filter(" fra, nOr , SWE ")
    assert parsed == {"FRA", "NOR", "SWE"}


def test_compute_athletes_returns_all_nationalities(monkeypatch):
    data = _form_data(
        [
            _athlete_result("A1", "Alpha", "FRA", "1"),
            _athlete_result("B1", "Bravo", "NOR", "2"),
            _athlete_result("C1", "Charlie", "SWE", "3"),
        ]
    )
    monkeypatch.setattr(
        form,
        "_get_wc_rows",
        lambda _cat, _season: [
            {"IBUId": "A1", "Rank": "1"},
            {"IBUId": "B1", "Rank": "2"},
            {"IBUId": "C1", "Rank": "3"},
        ],
    )

    athletes = form._compute_athletes(
        data,
        _args(),
        shoot_mode=False,
        result_mode=True,
    )

    assert athletes is not None
    assert {entry["ibu_id"] for entry in athletes} == {"A1", "B1", "C1"}
    assert {entry["nat"] for entry in athletes} == {"FRA", "NOR", "SWE"}


def test_render_form_table_nat_filter_keeps_global_rank(monkeypatch):
    data = form.FormData(
        season_id="2526",
        completed_race_ids=[],
        season_race_ids=[],
        race_payloads={},
        race_to_event={},
        race_is_relay={},
        race_discipline={},
        race_category={},
        race_headers=[],
        gender_cat="SW",
        gender_ibu_ids=set(),
        individual_race_ids=[],
        relay_race_ids=[],
        race_course_times={},
        relay_leg_course_times={},
        all_candidate_ids=[],
    )
    athletes = [
        {
            "ibu_id": "A1",
            "name": "Alpha",
            "nat": "FRA",
            "wc_rank": 1,
            "current_form": 1.0,
            "season_form": 1.0,
            "has_current_form": True,
            "ranks": {},
        },
        {
            "ibu_id": "B1",
            "name": "Bravo",
            "nat": "NOR",
            "wc_rank": 2,
            "current_form": 2.0,
            "season_form": 2.0,
            "has_current_form": True,
            "ranks": {},
        },
        {
            "ibu_id": "C1",
            "name": "Charlie",
            "nat": "SWE",
            "wc_rank": 3,
            "current_form": 3.0,
            "season_form": 3.0,
            "has_current_form": True,
            "ranks": {},
        },
    ]

    captured: dict = {}

    def fake_render_table(headers, rows, **kwargs):
        captured["headers"] = headers
        captured["rows"] = rows
        captured["kwargs"] = kwargs

    monkeypatch.setattr(form, "render_table", fake_render_table)

    rc = form._render_form_table(
        athletes,
        data,
        _args(),
        shoot_mode=False,
        nat_filter={"NOR"},
    )

    assert rc == 0
    assert captured["rows"] == [[2, "Bravo", "NOR", "2", "2.0", "2.0"]]


def test_render_combined_table_nat_filter_keeps_global_rank(monkeypatch):
    course_athletes = [
        {
            "ibu_id": "A1",
            "name": "Alpha",
            "nat": "FRA",
            "wc_rank": 1,
            "current_form": 1.0,
            "season_form": 1.0,
        },
        {
            "ibu_id": "B1",
            "name": "Bravo",
            "nat": "NOR",
            "wc_rank": 2,
            "current_form": 2.0,
            "season_form": 2.0,
        },
        {
            "ibu_id": "C1",
            "name": "Charlie",
            "nat": "SWE",
            "wc_rank": 3,
            "current_form": 3.0,
            "season_form": 3.0,
        },
    ]
    shoot_athletes = [
        {
            "ibu_id": "A1",
            "name": "Alpha",
            "nat": "FRA",
            "wc_rank": 1,
            "current_form": 99.0,
            "season_form": 99.0,
        },
        {
            "ibu_id": "B1",
            "name": "Bravo",
            "nat": "NOR",
            "wc_rank": 2,
            "current_form": 98.0,
            "season_form": 98.0,
        },
        {
            "ibu_id": "C1",
            "name": "Charlie",
            "nat": "SWE",
            "wc_rank": 3,
            "current_form": 97.0,
            "season_form": 97.0,
        },
    ]

    captured: dict = {}

    def fake_render_table(headers, rows, **kwargs):
        captured["headers"] = headers
        captured["rows"] = rows
        captured["kwargs"] = kwargs

    monkeypatch.setattr(form, "render_table", fake_render_table)

    rc = form._render_combined_table(
        course_athletes,
        shoot_athletes,
        _args(),
        nat_filter={"NOR"},
    )

    assert rc == 0
    assert captured["rows"] == [["2", "Bravo", "NOR", "2", "4", "2", "2"]]


def test_handle_form_rejects_empty_nat_filter(capsys):
    rc = form.handle_form(_args(nat=",,"))
    assert rc == 1
    assert "invalid --nat filter" in capsys.readouterr().err


def test_handle_form_standard_defaults_min_to_fifty(monkeypatch):
    seen_min_pct: list[int | None] = []

    monkeypatch.setattr(
        form,
        "_fetch_form_data",
        lambda _args, _cat: types.SimpleNamespace(gender_cat=_cat, season_id="2526"),
    )
    monkeypatch.setattr(
        form,
        "_compute_athletes",
        lambda _data, args, shoot_mode, **kwargs: (
            seen_min_pct.append(args.min_pct)
            or [
                {
                    "ibu_id": "A1",
                    "name": "Alpha",
                    "nat": "FRA",
                    "wc_rank": 1,
                    "current_form": 1.0,
                    "season_form": 1.0,
                    "has_current_form": True,
                    "ranks": {},
                }
            ]
        ),
    )
    monkeypatch.setattr(form, "_render_form_table", lambda *_a, **_k: 0)
    monkeypatch.setattr(form, "_format_section_title", lambda title, _args: title)

    rc = form.handle_form(_args(min_pct=None))

    assert rc == 0
    assert seen_min_pct == [50, 50, 50]


def test_handle_form_startlist_defaults_min_to_zero(monkeypatch):
    seen_min_pct: list[int | None] = []

    monkeypatch.setattr(
        form,
        "get_race_results",
        lambda _race_id: {
            "Competition": {"catId": "SW"},
            "Results": [{"IsTeam": False, "IBUId": "A1"}],
        },
    )
    monkeypatch.setattr(
        form,
        "_fetch_form_data",
        lambda _args, _cat: types.SimpleNamespace(gender_cat=_cat, season_id="2526"),
    )
    monkeypatch.setattr(
        form,
        "_compute_athletes",
        lambda _data, args, shoot_mode, **kwargs: (
            seen_min_pct.append(args.min_pct)
            or [
                {
                    "ibu_id": "A1",
                    "name": "Alpha",
                    "nat": "FRA",
                    "wc_rank": 1,
                    "current_form": 1.0,
                    "season_form": 1.0,
                    "has_current_form": True,
                    "ranks": {},
                }
            ]
        ),
    )
    monkeypatch.setattr(form, "_render_form_table", lambda *_a, **_k: 0)
    monkeypatch.setattr(form, "_render_combined_table", lambda *_a, **_k: 0)
    monkeypatch.setattr(form, "_format_section_title", lambda title, _args: title)

    rc = form.handle_form(_args(startlist="RSTART", min_pct=None))

    assert rc == 0
    assert seen_min_pct == [0, 0, 0]


def test_handle_form_startlist_keeps_explicit_min(monkeypatch):
    seen_min_pct: list[int | None] = []

    monkeypatch.setattr(
        form,
        "get_race_results",
        lambda _race_id: {
            "Competition": {"catId": "SW"},
            "Results": [{"IsTeam": False, "IBUId": "A1"}],
        },
    )
    monkeypatch.setattr(
        form,
        "_fetch_form_data",
        lambda _args, _cat: types.SimpleNamespace(gender_cat=_cat, season_id="2526"),
    )
    monkeypatch.setattr(
        form,
        "_compute_athletes",
        lambda _data, args, shoot_mode, **kwargs: (
            seen_min_pct.append(args.min_pct)
            or [
                {
                    "ibu_id": "A1",
                    "name": "Alpha",
                    "nat": "FRA",
                    "wc_rank": 1,
                    "current_form": 1.0,
                    "season_form": 1.0,
                    "has_current_form": True,
                    "ranks": {},
                }
            ]
        ),
    )
    monkeypatch.setattr(form, "_render_form_table", lambda *_a, **_k: 0)
    monkeypatch.setattr(form, "_render_combined_table", lambda *_a, **_k: 0)
    monkeypatch.setattr(form, "_format_section_title", lambda title, _args: title)

    rc = form.handle_form(_args(startlist="RSTART", min_pct=30))

    assert rc == 0
    assert seen_min_pct == [30, 30, 30]


def test_handle_form_standard_passes_nat_filter_to_all_tables(monkeypatch):
    seen_nat_filters: list[set[str] | None] = []

    monkeypatch.setattr(
        form,
        "_fetch_form_data",
        lambda _args, _cat: types.SimpleNamespace(gender_cat=_cat, season_id="2526"),
    )
    monkeypatch.setattr(
        form,
        "_compute_athletes",
        lambda _data, _args, shoot_mode, **kwargs: [
            {
                "ibu_id": "A1",
                "name": "Alpha",
                "nat": "FRA",
                "wc_rank": 1,
                "current_form": 1.0,
                "season_form": 1.0,
                "has_current_form": True,
                "ranks": {},
            }
        ],
    )
    monkeypatch.setattr(
        form,
        "_render_form_table",
        lambda *_a, **kwargs: seen_nat_filters.append(kwargs.get("nat_filter")) or 0,
    )
    monkeypatch.setattr(form, "_format_section_title", lambda title, _args: title)

    rc = form.handle_form(_args(nat="fra,nor"))

    assert rc == 0
    assert seen_nat_filters == [{"FRA", "NOR"}, {"FRA", "NOR"}, {"FRA", "NOR"}]


def test_handle_form_startlist_passes_nat_filter_to_all_tables(monkeypatch):
    seen_table_nat_filters: list[set[str] | None] = []
    seen_combined_nat_filters: list[set[str] | None] = []

    monkeypatch.setattr(
        form,
        "get_race_results",
        lambda _race_id: {
            "Competition": {"catId": "SW"},
            "Results": [{"IsTeam": False, "IBUId": "A1"}],
        },
    )
    monkeypatch.setattr(
        form,
        "_fetch_form_data",
        lambda _args, _cat: types.SimpleNamespace(gender_cat=_cat, season_id="2526"),
    )
    monkeypatch.setattr(
        form,
        "_compute_athletes",
        lambda _data, _args, shoot_mode, **kwargs: [
            {
                "ibu_id": "A1",
                "name": "Alpha",
                "nat": "FRA",
                "wc_rank": 1,
                "current_form": 1.0,
                "season_form": 1.0,
                "has_current_form": True,
                "ranks": {},
            }
        ],
    )
    monkeypatch.setattr(
        form,
        "_render_form_table",
        lambda *_a, **kwargs: (
            seen_table_nat_filters.append(kwargs.get("nat_filter")) or 0
        ),
    )
    monkeypatch.setattr(
        form,
        "_render_combined_table",
        lambda *_a, **kwargs: (
            seen_combined_nat_filters.append(kwargs.get("nat_filter")) or 0
        ),
    )
    monkeypatch.setattr(form, "_format_section_title", lambda title, _args: title)

    rc = form.handle_form(_args(startlist="RSTART", nat="fra,nor"))

    assert rc == 0
    assert seen_table_nat_filters == [{"FRA", "NOR"}, {"FRA", "NOR"}, {"FRA", "NOR"}]
    assert seen_combined_nat_filters == [{"FRA", "NOR"}]
