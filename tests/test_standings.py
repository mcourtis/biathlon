"""Tests for standings command handler."""

import argparse

from biathlon.commands import standings


def _args(**overrides) -> argparse.Namespace:
    base = {
        "season": "2526",
        "men": False,
        "country": False,
        "level": "1",
        "sort": "",
        "limit": 25,
        "format": "tsv",
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def _mock_country_cups(monkeypatch) -> None:
    monkeypatch.setattr(
        standings,
        "get_cups",
        lambda season_id: [
            {
                "CupId": "NATIONS_SW",
                "CatId": "SW",
                "Level": 1,
                "DisciplineId": "NC",
            },
            {
                "CupId": "NATIONS_SM",
                "CatId": "SM",
                "Level": 1,
                "DisciplineId": "NC",
            },
            {"CupId": "RELAY_SW", "CatId": "SW", "Level": 1, "DisciplineId": "RL"},
            {"CupId": "RELAY_SM", "CatId": "SM", "Level": 1, "DisciplineId": "RL"},
            {"CupId": "MIXED_MR", "CatId": "MX", "Level": 1, "DisciplineId": "MR"},
            {"CupId": "MIXED_SR", "CatId": "MX", "Level": 1, "DisciplineId": "SR"},
        ],
    )


def test_standings_country_mode_renders_nations_and_relay(monkeypatch):
    _mock_country_cups(monkeypatch)

    payloads = {
        "NATIONS_SW": {
            "Rows": [
                {"Nat": "FRA", "Name": "France", "Score": 910},
                {"Nat": "NOR", "Name": "Norway", "Score": 900},
                {"Nat": "GER", "Name": "Germany", "Score": 870},
            ]
        },
        "NATIONS_SM": {
            "Rows": [
                {"Nat": "NOR", "Name": "Norway", "Score": 930},
                {"Nat": "FRA", "Name": "France", "Score": 915},
                {"Nat": "GER", "Name": "Germany", "Score": 860},
            ]
        },
        "RELAY_SW": {
            "Rows": [
                {"Nat": "NOR", "Name": "Norway", "Score": 420},
                {"Nat": "FRA", "Name": "France", "Score": 390},
            ]
        },
        "RELAY_SM": {
            "Rows": [
                {"Nat": "FRA", "Name": "France", "Score": 410},
                {"Nat": "NOR", "Name": "Norway", "Score": 360},
            ]
        },
        "MIXED_MR": {
            "Rows": [
                {"Nat": "NOR", "Name": "Norway", "Score": 180},
                {"Nat": "FRA", "Name": "France", "Score": 170},
            ]
        },
        "MIXED_SR": {
            "Rows": [
                {"Nat": "NOR", "Name": "Norway", "Score": 70},
                {"Nat": "FRA", "Name": "France", "Score": 60},
            ]
        },
    }
    monkeypatch.setattr(
        standings, "get_cup_results", lambda cup_id: payloads.get(cup_id, {"Rows": []})
    )

    captured: dict = {}

    def fake_render_table(headers, rows, **kwargs):
        captured["headers"] = headers
        captured["rows"] = rows
        captured["kwargs"] = kwargs

    monkeypatch.setattr(standings, "render_table", fake_render_table)

    rc = standings.handle_standings(_args(country=True))

    assert rc == 0
    assert captured["headers"] == [
        "Position",
        "Country",
        "Women Nations Cup",
        "Men Nations Cup",
        "Women Relay",
        "Men Relay",
        "Mixed Relay",
    ]
    assert captured["rows"] == [
        [1, "FRA", 910, 915, 390, 410, 230],
        [2, "NOR", 900, 930, 420, 360, 250],
        [3, "GER", 870, 860, 0, 0, 0],
    ]
    assert captured["kwargs"]["column_separators"] == {2, 4}


def test_standings_country_mode_sort_women_relay(monkeypatch):
    _mock_country_cups(monkeypatch)

    payloads = {
        "NATIONS_SW": {
            "Rows": [
                {"Nat": "FRA", "Score": 910},
                {"Nat": "NOR", "Score": 900},
            ]
        },
        "NATIONS_SM": {
            "Rows": [
                {"Nat": "NOR", "Score": 930},
                {"Nat": "FRA", "Score": 915},
            ]
        },
        "RELAY_SW": {
            "Rows": [
                {"Nat": "NOR", "Score": 420},
                {"Nat": "FRA", "Score": 390},
            ]
        },
        "RELAY_SM": {
            "Rows": [
                {"Nat": "FRA", "Score": 410},
                {"Nat": "NOR", "Score": 360},
            ]
        },
        "MIXED_MR": {
            "Rows": [
                {"Nat": "NOR", "Score": 180},
                {"Nat": "FRA", "Score": 170},
            ]
        },
        "MIXED_SR": {
            "Rows": [
                {"Nat": "FRA", "Score": 160},
                {"Nat": "NOR", "Score": 70},
            ]
        },
    }
    monkeypatch.setattr(
        standings, "get_cup_results", lambda cup_id: payloads.get(cup_id, {"Rows": []})
    )

    captured: dict = {}

    def fake_render_table(headers, rows, **kwargs):
        captured["headers"] = headers
        captured["rows"] = rows
        captured["kwargs"] = kwargs

    monkeypatch.setattr(standings, "render_table", fake_render_table)

    rc = standings.handle_standings(_args(country=True, sort="women-relay"))

    assert rc == 0
    assert captured["headers"] == [
        "Position",
        "Country",
        "Women Nations Cup",
        "Men Nations Cup",
        "Women Relay",
        "Men Relay",
        "Mixed Relay",
    ]
    assert captured["rows"] == [
        [1, "NOR", 900, 930, 420, 360, 250],
        [2, "FRA", 910, 915, 390, 410, 330],
    ]
    assert captured["kwargs"]["column_separators"] == {2, 4}


def test_standings_country_mode_rejects_athlete_sort(capsys):
    rc = standings.handle_standings(_args(country=True, sort="sprint"))

    assert rc == 1
    assert "when using --country" in capsys.readouterr().err


def test_standings_country_mode_rejects_legacy_country_sort(capsys):
    rc = standings.handle_standings(_args(country=True, sort="nations"))

    assert rc == 1
    assert "women-nations" in capsys.readouterr().err


def test_standings_country_mode_pretty_adds_leader_markers(monkeypatch):
    _mock_country_cups(monkeypatch)
    payloads = {
        "NATIONS_SW": {
            "Rows": [{"Nat": "FRA", "Score": 100}, {"Nat": "NOR", "Score": 90}]
        },
        "NATIONS_SM": {
            "Rows": [{"Nat": "NOR", "Score": 110}, {"Nat": "FRA", "Score": 80}]
        },
        "RELAY_SW": {
            "Rows": [{"Nat": "NOR", "Score": 50}, {"Nat": "FRA", "Score": 40}]
        },
        "RELAY_SM": {
            "Rows": [{"Nat": "FRA", "Score": 60}, {"Nat": "NOR", "Score": 30}]
        },
        "MIXED_MR": {
            "Rows": [{"Nat": "NOR", "Score": 25}, {"Nat": "FRA", "Score": 20}]
        },
        "MIXED_SR": {
            "Rows": [{"Nat": "NOR", "Score": 15}, {"Nat": "FRA", "Score": 10}]
        },
    }
    monkeypatch.setattr(
        standings, "get_cup_results", lambda cup_id: payloads.get(cup_id, {"Rows": []})
    )
    monkeypatch.setattr(standings.Color, "enabled", classmethod(lambda cls: True))

    captured: dict = {}

    def fake_render_table(headers, rows, **kwargs):
        captured["rows"] = rows
        captured["kwargs"] = kwargs

    monkeypatch.setattr(standings, "render_table", fake_render_table)

    rc = standings.handle_standings(_args(country=True, format=""))

    assert rc == 0
    # FRA leads women nations + men relay; NOR leads men nations + women relay + mixed relay.
    assert captured["rows"][0][1].count(standings.DISCIPLINE_LEADER_MARKER) == 2
    assert captured["rows"][1][1].count(standings.DISCIPLINE_LEADER_MARKER) == 3
    assert captured["kwargs"]["cell_formatters"] is not None
    assert captured["kwargs"]["highlight_headers"] == [2]
    assert captured["kwargs"]["column_separators"] == {2, 4}

    cell_formatters = captured["kwargs"]["cell_formatters"]
    country_fmt = cell_formatters[1]
    women_nations_fmt = cell_formatters[2]
    men_nations_fmt = cell_formatters[3]
    women_relay_fmt = cell_formatters[4]
    men_relay_fmt = cell_formatters[5]
    mixed_relay_fmt = cell_formatters[6]

    assert country_fmt is not None
    assert women_nations_fmt is not None
    assert men_nations_fmt is not None
    assert women_relay_fmt is not None
    assert men_relay_fmt is not None
    assert mixed_relay_fmt is not None

    # Country column: both leader countries are highlighted and dot markers rendered.
    country_row0 = country_fmt(captured["rows"][0][1], 0)
    country_row1 = country_fmt(captured["rows"][1][1], 1)
    assert "\x1b[38;2;" in country_row0
    assert "\x1b[38;2;" in country_row1
    assert "●" in country_row0
    assert "●" in country_row1

    # Points columns: only the leading country's value for each column is highlighted.
    assert "\x1b[38;2;" in women_nations_fmt(str(captured["rows"][0][2]), 0)
    assert "\x1b[38;2;" not in women_nations_fmt(str(captured["rows"][1][2]), 1)

    assert "\x1b[38;2;" not in men_nations_fmt(str(captured["rows"][0][3]), 0)
    assert "\x1b[38;2;" in men_nations_fmt(str(captured["rows"][1][3]), 1)

    assert "\x1b[38;2;" not in women_relay_fmt(str(captured["rows"][0][4]), 0)
    assert "\x1b[38;2;" in women_relay_fmt(str(captured["rows"][1][4]), 1)

    assert "\x1b[38;2;" in men_relay_fmt(str(captured["rows"][0][5]), 0)
    assert "\x1b[38;2;" not in men_relay_fmt(str(captured["rows"][1][5]), 1)

    assert "\x1b[38;2;" not in mixed_relay_fmt(str(captured["rows"][0][6]), 0)
    assert "\x1b[38;2;" in mixed_relay_fmt(str(captured["rows"][1][6]), 1)


def test_standings_athlete_mode_rejects_country_sort(capsys):
    rc = standings.handle_standings(_args(country=False, sort="relay"))

    assert rc == 1
    assert "or use --country" in capsys.readouterr().err


def _mock_athlete_cups(monkeypatch) -> None:
    monkeypatch.setattr(
        standings,
        "get_cups",
        lambda season_id: [
            {"CupId": "TS_SW", "CatId": "SW", "Level": 1, "DisciplineId": "TS"},
            {"CupId": "SP_SW", "CatId": "SW", "Level": 1, "DisciplineId": "SP"},
            {"CupId": "PU_SW", "CatId": "SW", "Level": 1, "DisciplineId": "PU"},
            {"CupId": "IN_SW", "CatId": "SW", "Level": 1, "DisciplineId": "IN"},
            {"CupId": "MS_SW", "CatId": "SW", "Level": 1, "DisciplineId": "MS"},
        ],
    )


def test_standings_athlete_mode_sets_column_separators(monkeypatch):
    _mock_athlete_cups(monkeypatch)
    payloads = {
        "TS_SW": {
            "Rows": [
                {"IBUId": "A", "Name": "A One", "Nat": "NOR", "Score": 200},
                {"IBUId": "B", "Name": "B Two", "Nat": "FRA", "Score": 180},
            ]
        },
        "SP_SW": {
            "Rows": [
                {"IBUId": "A", "Score": 90},
                {"IBUId": "B", "Score": 75},
            ]
        },
        "PU_SW": {"Rows": []},
        "IN_SW": {"Rows": []},
        "MS_SW": {"Rows": []},
    }
    monkeypatch.setattr(
        standings, "get_cup_results", lambda cup_id: payloads.get(cup_id, {"Rows": []})
    )

    captured: dict = {}

    def fake_render_table(headers, rows, **kwargs):
        captured["headers"] = headers
        captured["rows"] = rows
        captured["kwargs"] = kwargs

    monkeypatch.setattr(standings, "render_table", fake_render_table)

    rc_total = standings.handle_standings(_args(sort="total", format=""))
    assert rc_total == 0
    assert captured["kwargs"]["column_separators"] == {3, 4}

    rc_disc = standings.handle_standings(_args(sort="sprint", format=""))
    assert rc_disc == 0
    assert captured["kwargs"]["column_separators"] == {4, 5}
