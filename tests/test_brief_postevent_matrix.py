"""Matrix and delta-row contract tests for brief postevent."""

import argparse

from biathlon.commands import brief


def test_postevent_section_matrix_has_full_event_coverage():
    expected_sections = set(brief.POSTEVENT_SECTION_ORDER)

    assert set(brief.POSTEVENT_SECTION_TITLES) == expected_sections
    assert set(brief.POSTEVENT_SECTION_MATRIX) == expected_sections

    for section_id in brief.POSTEVENT_SECTION_ORDER:
        row = brief.POSTEVENT_SECTION_MATRIX[section_id]
        assert set(row) == set(brief.POSTEVENT_CATEGORY_CODES)
        for category_code in brief.POSTEVENT_CATEGORY_CODES:
            assert isinstance(row[category_code], bool)


def test_postevent_matrix_sample_cells_match_spec():
    assert brief._postevent_section_enabled(brief.POSTEVENT_SECTION_EVENT_FACTS, "WC")
    assert brief._postevent_section_enabled(brief.POSTEVENT_SECTION_EVENT_AGENDA, "WCH")
    assert brief._postevent_section_enabled(
        brief.POSTEVENT_SECTION_LAST_10_EDITIONS, "OWG"
    )
    assert brief._postevent_section_enabled(
        brief.POSTEVENT_SECTION_BEST_PERFORMANCES, "WC"
    )
    assert brief._postevent_section_enabled(
        brief.POSTEVENT_SECTION_RACE_MILESTONES, "WCH"
    )
    assert brief._postevent_section_enabled(
        brief.POSTEVENT_SECTION_WIN_MILESTONES, "OWG"
    )
    assert brief._postevent_section_enabled(
        brief.POSTEVENT_SECTION_ATHLETE_STANDINGS, "WC"
    )
    assert not brief._postevent_section_enabled(
        brief.POSTEVENT_SECTION_ATHLETE_STANDINGS, "WCH"
    )
    assert not brief._postevent_section_enabled(
        brief.POSTEVENT_SECTION_ATHLETE_STANDINGS, "OWG"
    )
    assert brief._postevent_section_enabled(
        brief.POSTEVENT_SECTION_RELAY_STANDINGS, "WC"
    )
    assert not brief._postevent_section_enabled(
        brief.POSTEVENT_SECTION_RELAY_STANDINGS, "WCH"
    )
    assert not brief._postevent_section_enabled(
        brief.POSTEVENT_SECTION_RELAY_STANDINGS, "OWG"
    )
    assert brief._postevent_section_enabled(brief.POSTEVENT_SECTION_NATIONS_CUP, "WC")
    assert not brief._postevent_section_enabled(
        brief.POSTEVENT_SECTION_NATIONS_CUP, "WCH"
    )
    assert not brief._postevent_section_enabled(
        brief.POSTEVENT_SECTION_NATIONS_CUP, "OWG"
    )
    assert brief._postevent_section_enabled(
        brief.POSTEVENT_SECTION_DECORATED_VENUE, "WC"
    )
    assert brief._postevent_section_enabled(
        brief.POSTEVENT_SECTION_DECORATED_EVENT_TYPE, "OWG"
    )
    assert brief._postevent_section_enabled(
        brief.POSTEVENT_SECTION_DECORATED_MAJOR_EVENTS, "WC"
    )
    assert brief._postevent_section_enabled(
        brief.POSTEVENT_SECTION_DECORATED_MAJOR_EVENTS, "WCH"
    )
    assert brief._postevent_section_enabled(
        brief.POSTEVENT_SECTION_DECORATED_MAJOR_EVENTS, "OWG"
    )


def test_handle_brief_postevent_adds_blank_lines_between_top_level_sections(
    monkeypatch, capsys
):
    event_id = "E1"
    monkeypatch.setattr(
        brief,
        "_find_event_by_id",
        lambda value: {
            "EventId": value,
            "SeasonId": "2526",
            "Level": 1,
            "Organizer": "Kontiolahti",
        },
    )
    monkeypatch.setattr(
        brief,
        "get_races",
        lambda value: [
            {
                "RaceId": "R1",
                "StartTime": "2026-03-05T10:00:00Z",
                "catId": "SW",
                "DisciplineId": "IN",
            }
        ],
    )
    monkeypatch.setattr(
        brief,
        "get_race_results",
        lambda value: {
            "Competition": {
                "DisciplineId": "IN",
                "catId": "SW",
                "StartTime": "2026-03-05T10:00:00Z",
            },
            "Results": [
                {
                    "IsTeam": False,
                    "Rank": "1",
                    "IBUId": "A",
                    "Nat": "FRA",
                    "Name": "Alpha",
                }
            ],
            "SportEvt": {"SeasonId": "2526"},
        },
    )
    monkeypatch.setattr(
        brief,
        "_resolve_event_type_and_venue",
        lambda current_event, first_race_id: ("WCH", "Kontiolahti", {}),
    )
    monkeypatch.setattr(
        brief, "_resolve_event_country", lambda *args, **kwargs: "Finland"
    )
    monkeypatch.setattr(brief, "_collect_venue_level1_events", lambda venue_name: [])
    monkeypatch.setattr(
        brief,
        "_count_venue_event_editions",
        lambda venue_name, venue_events, reference_date: (25, 6, 0),
    )
    monkeypatch.setattr(
        brief,
        "_compute_preevent_snapshot_standings",
        lambda *args, **kwargs: brief._empty_standings_snapshot(),
    )
    monkeypatch.setattr(
        brief, "_latest_completed_level1_event_id", lambda season_id: ""
    )
    monkeypatch.setattr(
        brief,
        "_postevent_section_enabled",
        lambda section_id, category_code: section_id
        in {
            brief.POSTEVENT_SECTION_EVENT_FACTS,
            brief.POSTEVENT_SECTION_EVENT_AGENDA,
        },
    )

    rc = brief.handle_brief_postevent(argparse.Namespace(event=event_id, format="tsv"))

    assert rc == 0
    out = capsys.readouterr().out
    assert (
        "## Event Facts\n\n"
        "Country\tWC Editions\tWCH Editions\tOWG Editions\n"
        "Finland\t25\t6\t0\n\n\n"
        "## Event Agenda\n\n"
    ) in out


def test_handle_brief_postevent_renders_decorated_major_events_section(monkeypatch):
    event_id = "E1"
    monkeypatch.setattr(
        brief,
        "_find_event_by_id",
        lambda value: {
            "EventId": value,
            "SeasonId": "2526",
            "Level": 1,
            "Organizer": "Kontiolahti",
        },
    )
    monkeypatch.setattr(
        brief,
        "get_races",
        lambda value: [
            {
                "RaceId": "R1",
                "StartTime": "2026-03-05T10:00:00Z",
                "catId": "SW",
                "DisciplineId": "IN",
            }
        ],
    )
    monkeypatch.setattr(
        brief,
        "get_race_results",
        lambda value: {
            "Competition": {
                "DisciplineId": "IN",
                "catId": "SW",
                "StartTime": "2026-03-05T10:00:00Z",
            },
            "Results": [
                {
                    "IsTeam": False,
                    "Rank": "1",
                    "IBUId": "A",
                    "Nat": "NOR",
                    "Name": "Alpha",
                }
            ],
            "SportEvt": {"SeasonId": "2526"},
        },
    )
    monkeypatch.setattr(
        brief,
        "_resolve_event_type_and_venue",
        lambda current_event, first_race_id: ("WCH", "Kontiolahti", {}),
    )
    monkeypatch.setattr(brief, "_resolve_event_country", lambda *args, **kwargs: "FIN")
    monkeypatch.setattr(brief, "_collect_venue_level1_events", lambda venue_name: [])
    monkeypatch.setattr(
        brief,
        "_count_venue_event_editions",
        lambda venue_name, venue_events, reference_date: (25, 6, 0),
    )
    monkeypatch.setattr(
        brief,
        "_compute_preevent_snapshot_standings",
        lambda *args, **kwargs: brief._empty_standings_snapshot(),
    )
    monkeypatch.setattr(
        brief, "_latest_completed_level1_event_id", lambda season_id: ""
    )
    monkeypatch.setattr(
        brief,
        "_postevent_section_enabled",
        lambda section_id, category_code: (
            section_id == brief.POSTEVENT_SECTION_DECORATED_MAJOR_EVENTS
        ),
    )

    major_calls: list[dict] = []

    def fake_build_major(**kwargs):
        major_calls.append(kwargs)
        if len(major_calls) == 1:
            return [], []
        return [["1", "Alpha", "NOR", "F", "1", "0", "0", "1", "1"]], []

    monkeypatch.setattr(
        brief, "_build_major_events_decorated_athlete_rows", fake_build_major
    )

    rendered_titles: list[str] = []

    def fake_render(
        title,
        before_rows,
        after_rows,
        after_row_styles,
        args,
        per_gender_limit=10,
        gender_filter=None,
    ):
        rendered_titles.append(title)
        assert before_rows == []
        assert after_rows == [["1", "Alpha", "NOR", "F", "1", "0", "0", "1", "1"]]
        assert per_gender_limit == 10

    monkeypatch.setattr(
        brief, "_render_postevent_decorated_delta_split_tables", fake_render
    )

    rc = brief.handle_brief_postevent(argparse.Namespace(event=event_id, format="tsv"))

    assert rc == 0
    assert len(major_calls) == 2
    assert major_calls[0]["exclude_event_ids"] == {event_id}
    assert "exclude_event_ids" not in major_calls[1]
    assert rendered_titles == ["Most Decorated Athletes at World Cup / WCH / OWG"]


def test_build_postevent_athlete_delta_rows_reports_rank_and_points_changes():
    before_rows = [
        {"Rank": "1", "IBUId": "A", "Name": "Alpha", "Nat": "NOR", "Score": "100"},
        {"Rank": "2", "IBUId": "B", "Name": "Bravo", "Nat": "FRA", "Score": "90"},
    ]
    after_rows = [
        {"Rank": "1", "IBUId": "B", "Name": "Bravo", "Nat": "FRA", "Score": "130"},
        {"Rank": "2", "IBUId": "C", "Name": "Charlie", "Nat": "GER", "Score": "95"},
        {"Rank": "3", "IBUId": "A", "Name": "Alpha", "Nat": "NOR", "Score": "100"},
    ]

    rows, styles = brief._build_postevent_athlete_delta_rows(
        after_rows, before_rows, limit=3
    )

    assert rows[0] == ["1 (+1)", "Bravo", "FRA", "130 (+40)"]
    assert rows[1] == ["2 (new)", "Charlie", "GER", "95 (new)"]
    assert rows[2] == ["3 (-2)", "Alpha", "NOR", "100 (+0)"]
    assert styles == ["highlight_plain", "highlight_plain", "highlight_plain"]


def test_build_postevent_athlete_delta_rows_uses_full_previous_standing():
    before_rows = []
    for rank in range(1, 13):
        score = float(900 - rank * 20)
        row = {
            "Rank": str(rank),
            "IBUId": f"P{rank}",
            "Name": f"Athlete {rank}",
            "Nat": "NOR",
            "Score": str(score),
        }
        before_rows.append(row)

    # Athlete previously at rank 12 enters current top 10.
    after_rows = [
        {
            "Rank": "8",
            "IBUId": "P12",
            "Name": "Athlete 12",
            "Nat": "NOR",
            "Score": "700",
        }
    ]

    rows, styles = brief._build_postevent_athlete_delta_rows(
        after_rows, before_rows, limit=10
    )

    assert rows == [["8 (+4)", "Athlete 12", "NOR", "700 (+40)"]]
    assert styles == ["highlight_plain"]


def test_align_postevent_athlete_merged_delta_cells_aligns_subfields():
    rows = [
        ["1 (=)", "JEANMONNOT Lou", "FRA", "848 (+130)"],
        ["10 (-2)", "WIERER Dorothea", "ITA", "456 (+57)"],
        ["3 (+3)", "MAGNUSSON Anna", "SWE", "585 (+84)"],
    ]

    aligned = brief._align_postevent_athlete_merged_delta_cells(rows)

    assert aligned[0][0] == " 1 ( =)"
    assert aligned[1][0] == "10 (-2)"
    assert aligned[2][0] == " 3 (+3)"
    assert aligned[0][3] == "848 (+130)"
    assert aligned[1][3] == "456 ( +57)"
    assert aligned[2][3] == "585 ( +84)"


def test_build_postevent_country_delta_rows_reports_rank_and_points_changes():
    before_rows = [
        {"Rank": "1", "Nat": "NOR", "Name": "Norway", "Score": "250.5"},
        {"Rank": "2", "Nat": "FRA", "Name": "France", "Score": "200.0"},
    ]
    after_rows = [
        {"Rank": "1", "Nat": "FRA", "Name": "France", "Score": "280.0"},
        {"Rank": "2", "Nat": "NOR", "Name": "Norway", "Score": "260.5"},
    ]

    rows, styles = brief._build_postevent_country_delta_rows(
        after_rows, before_rows, limit=2
    )

    assert rows[0] == ["1 (+1)", "France", "280 (+80)"]
    assert rows[1] == ["2 (-1)", "Norway", "260.5 (+10)"]
    assert styles == ["highlight_plain", "highlight_plain"]


def test_event_nations_points_from_completed_races_skips_mass_start_and_handles_mixed():
    completed_races = [
        (
            "W_IN",
            {
                "Competition": {"DisciplineId": "IN", "catId": "SW"},
                "Results": [
                    {"IsTeam": False, "Nat": "FRA", "NC": "160"},
                    {"IsTeam": False, "Nat": "NOR", "NC": "154"},
                ],
            },
        ),
        (
            "W_MS",
            {
                "Competition": {"DisciplineId": "MS", "catId": "SW"},
                "Results": [{"IsTeam": False, "Nat": "FRA", "NC": "60"}],
            },
        ),
        (
            "W_RL",
            {
                "Competition": {"DisciplineId": "RL", "catId": "SW"},
                "Results": [{"IsTeam": True, "Nat": "FRA", "NC": "390"}],
            },
        ),
        (
            "MX_MR",
            {
                "Competition": {"DisciplineId": "MR", "catId": "MX"},
                "Results": [{"IsTeam": True, "Nat": "SWE", "NC": "210"}],
            },
        ),
    ]

    points = brief._event_nations_points_from_completed_races(completed_races)

    assert points == {
        "SW": {"FRA": 550.0, "NOR": 154.0, "SWE": 210.0},
        "SM": {"SWE": 210.0},
    }


def test_event_nations_points_from_completed_races_falls_back_to_nc_table():
    completed_races = [
        (
            "M_SP",
            {
                "Competition": {"DisciplineId": "SP", "catId": "SM"},
                "Results": [{"IsTeam": False, "Nat": "NOR", "Rank": "1"}],
            },
        ),
        (
            "MX_SR",
            {
                "Competition": {"DisciplineId": "SR", "catId": "MX"},
                "Results": [{"IsTeam": True, "Nat": "FRA", "Rank": "2"}],
            },
        ),
    ]

    points = brief._event_nations_points_from_completed_races(completed_races)

    assert points["SM"]["NOR"] == 160.0
    assert points["SW"]["FRA"] == 195.0
    assert points["SM"]["FRA"] == 195.0


def test_derive_postevent_nations_before_rows_subtracts_current_event_points():
    after_nations = {
        "SW": [
            {"Rank": "1", "Nat": "FRA", "Name": "France", "Score": "5944"},
            {"Rank": "2", "Nat": "SWE", "Name": "Sweden", "Score": "5724"},
        ],
        "SM": [
            {"Rank": "1", "Nat": "NOR", "Name": "Norway", "Score": "6232"},
            {"Rank": "2", "Nat": "FRA", "Name": "France", "Score": "6045"},
        ],
        "ALL": [],
    }
    completed_races = [
        (
            "W_IN",
            {
                "Competition": {"DisciplineId": "IN", "catId": "SW"},
                "Results": [
                    {"IsTeam": False, "Nat": "FRA", "NC": "160"},
                    {"IsTeam": False, "Nat": "SWE", "NC": "154"},
                ],
            },
        ),
        (
            "W_RL",
            {
                "Competition": {"DisciplineId": "RL", "catId": "SW"},
                "Results": [{"IsTeam": True, "Nat": "FRA", "NC": "390"}],
            },
        ),
        (
            "M_IN",
            {
                "Competition": {"DisciplineId": "IN", "catId": "SM"},
                "Results": [
                    {"IsTeam": False, "Nat": "NOR", "NC": "160"},
                    {"IsTeam": False, "Nat": "FRA", "NC": "154"},
                ],
            },
        ),
    ]

    before = brief._derive_postevent_nations_before_rows(after_nations, completed_races)

    assert before["SW"][:2] == [
        {"Rank": 1, "Name": "Sweden", "Nat": "SWE", "Score": "5570"},
        {"Rank": 2, "Name": "France", "Nat": "FRA", "Score": "5394"},
    ]
    assert before["SM"][:2] == [
        {"Rank": 1, "Name": "Norway", "Nat": "NOR", "Score": "6072"},
        {"Rank": 2, "Name": "France", "Nat": "FRA", "Score": "5891"},
    ]
    assert before["ALL"][:2] == [
        {"Rank": 1, "Name": "France", "Nat": "FRA", "Score": "11285.0"},
        {"Rank": 2, "Name": "Norway", "Nat": "NOR", "Score": "6072.0"},
    ]


def test_derive_postevent_nations_before_rows_clamps_negative_values_to_zero():
    after_nations = {
        "SW": [{"Rank": "1", "Nat": "FRA", "Name": "France", "Score": "100"}],
        "SM": [],
        "ALL": [],
    }
    completed_races = [
        (
            "W_IN",
            {
                "Competition": {"DisciplineId": "IN", "catId": "SW"},
                "Results": [{"IsTeam": False, "Nat": "FRA", "NC": "160"}],
            },
        )
    ]

    before = brief._derive_postevent_nations_before_rows(after_nations, completed_races)

    assert before["SW"] == [{"Rank": 1, "Name": "France", "Nat": "FRA", "Score": "0"}]


def test_fetch_live_postevent_standings_limit_zero_keeps_full_country_rows(monkeypatch):
    def fake_rows() -> list[dict]:
        rows: list[dict] = []
        for rank in range(1, 13):
            rows.append(
                {
                    "Rank": str(rank),
                    "Nat": f"N{rank:02d}",
                    "Name": f"Team {rank}",
                    "Score": str(200 - rank),
                }
            )
        return rows

    monkeypatch.setattr(
        brief,
        "_fetch_live_athlete_cup_rows",
        lambda season_id: {"SW": {"TS": []}, "SM": {"TS": []}},
    )
    monkeypatch.setattr(
        brief,
        "_find_level1_cup_id",
        lambda season_id, cat_id, discipline_id: f"{cat_id}_{discipline_id}",
    )
    monkeypatch.setattr(
        brief,
        "_find_level1_mixed_relay_cup_id",
        lambda season_id: "MX_RL",
    )
    monkeypatch.setattr(brief, "_fetch_cup_rows", lambda cup_id: fake_rows())

    snapshot = brief._fetch_live_postevent_standings("2526", limit=0)

    assert len(snapshot["relay"]["SW"]) == 12
    assert len(snapshot["relay"]["SM"]) == 12
    assert len(snapshot["relay"]["MX"]) == 12
    assert len(snapshot["nations"]["SW"]) == 12
    assert len(snapshot["nations"]["SM"]) == 12
    assert any(row.get("Nat") == "N11" for row in snapshot["relay"]["SW"])
    assert any(row.get("Nat") == "N11" for row in snapshot["nations"]["SW"])


def test_align_postevent_country_merged_delta_cells_aligns_subfields():
    rows = [
        ["1 (=)", "France", "1288.5 (+130)"],
        ["10 (-2)", "Norway", "456 (+57)"],
        ["3 (+3)", "Sweden", "585 (+84)"],
    ]

    aligned = brief._align_postevent_country_merged_delta_cells(rows)

    assert aligned[0][0] == " 1 ( =)"
    assert aligned[1][0] == "10 (-2)"
    assert aligned[2][0] == " 3 (+3)"
    assert aligned[0][2] == "1288.5 (+130)"
    assert aligned[1][2] == "   456 ( +57)"
    assert aligned[2][2] == "   585 ( +84)"


def test_rank_delta_text_uses_equals_for_no_change():
    assert brief._rank_delta_text(2, 2) == "="


def test_rank_delta_cell_formatter_colors_signed_values(monkeypatch):
    monkeypatch.setattr(brief.Color, "enabled", classmethod(lambda cls: True))

    assert brief._format_postevent_rank_delta_cell("=", 0) == "="
    plus = brief._format_postevent_rank_delta_cell("+2", 0)
    minus = brief._format_postevent_rank_delta_cell("-1", 0)
    assert "\033[" in plus and "+2" in plus
    assert "\033[" in minus and "-1" in minus


def test_inline_delta_cell_formatter_colors_signed_values(monkeypatch):
    monkeypatch.setattr(brief.Color, "enabled", classmethod(lambda cls: True))

    assert brief._format_postevent_inline_delta_cell("1 (=)", 0) == "1 (=)"
    plus = brief._format_postevent_inline_delta_cell("848 (+130)", 0)
    minus = brief._format_postevent_inline_delta_cell("4 (-2)", 0)
    assert "\033[" in plus and "(+130)" in plus
    assert "\033[" in minus and "(-2)" in minus


def test_rank_inline_delta_cell_formatter_colors_new_blue(monkeypatch):
    monkeypatch.setattr(brief.Color, "enabled", classmethod(lambda cls: True))

    formatted = brief._format_postevent_rank_inline_delta_cell("3 (new)", 0)
    assert "\033[" in formatted and "(new)" in formatted


def test_render_postevent_athlete_standings_uses_delta_cell_formatters(monkeypatch):
    captured: list[tuple[list[str], list[list[str]], dict]] = []

    def fake_render_table(headers, rows, **kwargs):
        captured.append((headers, rows, kwargs))

    monkeypatch.setattr(brief, "render_table", fake_render_table)
    brief._render_postevent_athlete_standings(
        argparse.Namespace(format="pretty"),
        before_standings={
            "athlete": {"SW": {"TS": []}, "SM": {"TS": []}},
            "relay": {},
            "nations": {},
        },
        after_standings={
            "athlete": {
                "SW": {
                    "TS": [
                        {
                            "Rank": "1",
                            "IBUId": "A",
                            "Name": "Alpha",
                            "Nat": "FRA",
                            "Score": "100",
                        }
                    ]
                },
                "SM": {"TS": []},
            },
            "relay": {},
            "nations": {},
        },
        disciplines_raced=set(),
    )

    assert captured
    _headers, rows, kwargs = captured[0]
    assert kwargs.get("row_styles") is None
    assert (
        kwargs.get("cell_formatters")[0]
        is brief._format_postevent_rank_inline_delta_cell
    )
    assert kwargs.get("cell_formatters")[1] is brief._format_leader_markers
    assert callable(kwargs.get("cell_formatters")[3])
    assert brief.GENERAL_LEADER_MARKER in rows[0][1]


def test_render_postevent_relay_standings_uses_merged_delta_cell_formatters(
    monkeypatch,
):
    captured: list[tuple[list[str], list[list[str]], dict]] = []

    def fake_render_table(headers, rows, **kwargs):
        captured.append((headers, rows, kwargs))

    monkeypatch.setattr(brief, "render_table", fake_render_table)
    brief._render_postevent_relay_standings(
        argparse.Namespace(format="pretty"),
        before_standings={
            "relay": {
                "SW": [{"Rank": "1", "Nat": "NOR", "Name": "Norway", "Score": "100"}],
                "SM": [],
                "MX": [],
            }
        },
        after_standings={
            "relay": {
                "SW": [{"Rank": "1", "Nat": "NOR", "Name": "Norway", "Score": "120"}],
                "SM": [],
                "MX": [],
            }
        },
    )

    assert captured
    headers, _rows, kwargs = captured[0]
    assert headers == ["Rank", "Team", "Points"]
    assert (
        kwargs.get("cell_formatters")[0]
        is brief._format_postevent_rank_inline_delta_cell
    )
    points_formatter = kwargs.get("cell_formatters")[2]
    assert callable(points_formatter)
    assert points_formatter is not brief._format_postevent_inline_delta_cell
    assert points_formatter("120 (+20)", 0) == "120 (+20)"


def test_render_postevent_nations_standings_uses_merged_delta_cell_formatters(
    monkeypatch,
):
    captured: list[tuple[list[str], list[list[str]], dict]] = []

    def fake_render_table(headers, rows, **kwargs):
        captured.append((headers, rows, kwargs))

    monkeypatch.setattr(brief, "render_table", fake_render_table)
    brief._render_postevent_nations_standings(
        argparse.Namespace(format="pretty"),
        before_standings={
            "nations": {
                "SW": [{"Rank": "1", "Nat": "FRA", "Name": "France", "Score": "200"}],
                "SM": [],
            }
        },
        after_standings={
            "nations": {
                "SW": [{"Rank": "1", "Nat": "FRA", "Name": "France", "Score": "240"}],
                "SM": [],
            }
        },
    )

    assert captured
    headers, _rows, kwargs = captured[0]
    assert headers == ["Rank", "Team", "Points"]
    assert (
        kwargs.get("cell_formatters")[0]
        is brief._format_postevent_rank_inline_delta_cell
    )
    points_formatter = kwargs.get("cell_formatters")[2]
    assert callable(points_formatter)
    assert points_formatter is not brief._format_postevent_inline_delta_cell
    assert points_formatter("240 (+40)", 0) == "240 (+40)"


def test_render_postevent_best_performances_consolidates_rows_by_athlete(monkeypatch):
    captured: list[tuple[list[str], list[list[str]], dict]] = []

    def fake_render_table(headers, rows, **kwargs):
        captured.append((headers, rows, kwargs))

    monkeypatch.setattr(brief, "render_table", fake_render_table)
    monkeypatch.setattr(brief, "_prefetch_bios", lambda ibu_ids: {})
    brief._render_postevent_best_performances(
        argparse.Namespace(format="tsv"),
        completed_races=[
            (
                "R1",
                {
                    "Competition": {
                        "DisciplineId": "SP",
                        "catId": "SW",
                        "Description": "Sprint",
                        "StartTime": "2026-02-01T10:00:00Z",
                    },
                    "Results": [
                        {
                            "IsTeam": False,
                            "IBUId": "W2",
                            "Name": "Bravo",
                            "Nat": "FRA",
                            "Rank": "1",
                            "IRM": "",
                            "TotalTime": "25:00.0",
                        },
                        {
                            "IsTeam": False,
                            "IBUId": "W1",
                            "Name": "Alpha",
                            "Nat": "NOR",
                            "Rank": "2",
                            "IRM": "",
                            "TotalTime": "25:20.0",
                        },
                    ],
                },
            ),
            (
                "R2",
                {
                    "Competition": {
                        "DisciplineId": "PU",
                        "catId": "SW",
                        "Description": "Pursuit",
                        "StartTime": "2026-02-02T10:00:00Z",
                    },
                    "Results": [
                        {
                            "IsTeam": False,
                            "IBUId": "W1",
                            "Name": "Alpha",
                            "Nat": "NOR",
                            "Rank": "1",
                            "IRM": "",
                            "TotalTime": "30:00.0",
                        },
                        {
                            "IsTeam": False,
                            "IBUId": "W2",
                            "Name": "Bravo",
                            "Nat": "FRA",
                            "Rank": "3",
                            "IRM": "",
                            "TotalTime": "31:00.0",
                        },
                    ],
                },
            ),
            (
                "R3",
                {
                    "Competition": {
                        "DisciplineId": "SP",
                        "catId": "SM",
                        "Description": "Sprint",
                        "StartTime": "2026-02-03T10:00:00Z",
                    },
                    "Results": [
                        {
                            "IsTeam": False,
                            "IBUId": "M1",
                            "Name": "Charlie",
                            "Nat": "GER",
                            "Rank": "1",
                            "IRM": "",
                            "TotalTime": "24:50.0",
                        },
                        {
                            "IsTeam": False,
                            "IBUId": "M2",
                            "Name": "Delta",
                            "Nat": "SWE",
                            "Rank": "2",
                            "IRM": "",
                            "TotalTime": "25:10.0",
                        },
                    ],
                },
            ),
            (
                "R4",
                {
                    "Competition": {
                        "DisciplineId": "PU",
                        "catId": "SM",
                        "Description": "Pursuit",
                        "StartTime": "2026-02-04T10:00:00Z",
                    },
                    "Results": [
                        {
                            "IsTeam": False,
                            "IBUId": "M1",
                            "Name": "Charlie",
                            "Nat": "GER",
                            "Rank": "2",
                            "IRM": "",
                            "TotalTime": "29:55.0",
                        },
                        {
                            "IsTeam": False,
                            "IBUId": "M2",
                            "Name": "Delta",
                            "Nat": "SWE",
                            "Rank": "1",
                            "IRM": "",
                            "TotalTime": "29:40.0",
                        },
                    ],
                },
            ),
        ],
        all_results_cache={"W1": [], "W2": [], "M1": [], "M2": []},
        race_start_cache={},
        output_format="tsv",
    )

    assert len(captured) == 2

    women_headers, women_rows, women_kwargs = captured[0]
    assert women_headers == [
        "Athlete",
        "Nat",
        "Milestone",
        "Rank",
        "Previous Best",
        "Race",
        "Race ID",
    ]
    assert len(women_rows) == 4
    assert [row[0] for row in women_rows] == ["Alpha", "Alpha", "Bravo", "Bravo"]
    assert women_kwargs.get("column_separators") == {2, 5}
    women_race_cells = [row[5] for row in women_rows]
    women_race_ids = [row[6] for row in women_rows]
    assert "Sprint" in women_race_cells
    assert "Pursuit" in women_race_cells
    assert "R1" in women_race_ids
    assert "R2" in women_race_ids
    assert all("# " not in cell and " — " not in cell for cell in women_race_cells)

    men_headers, men_rows, men_kwargs = captured[1]
    assert men_headers == [
        "Athlete",
        "Nat",
        "Milestone",
        "Rank",
        "Previous Best",
        "Race",
        "Race ID",
    ]
    assert len(men_rows) == 4
    assert [row[0] for row in men_rows] == ["Charlie", "Charlie", "Delta", "Delta"]
    assert men_kwargs.get("column_separators") == {2, 5}
    men_race_cells = [row[5] for row in men_rows]
    men_race_ids = [row[6] for row in men_rows]
    assert "Sprint" in men_race_cells
    assert "Pursuit" in men_race_cells
    assert "R3" in men_race_ids
    assert "R4" in men_race_ids


def test_render_postevent_best_performances_splits_indiv_and_team_races(
    monkeypatch, capsys
):
    captured: list[tuple[list[str], list[list[str]], dict]] = []

    def fake_render_table(headers, rows, **kwargs):
        captured.append((headers, rows, kwargs))

    monkeypatch.setattr(brief, "render_table", fake_render_table)
    monkeypatch.setattr(brief, "_prefetch_bios", lambda ibu_ids: {})

    brief._render_postevent_best_performances(
        argparse.Namespace(format="tsv"),
        completed_races=[
            (
                "R1",
                {
                    "Competition": {
                        "DisciplineId": "SP",
                        "catId": "SW",
                        "Description": "Sprint",
                        "StartTime": "2026-02-01T10:00:00Z",
                    },
                    "Results": [
                        {
                            "IsTeam": False,
                            "IBUId": "A",
                            "Name": "Alpha",
                            "Nat": "FRA",
                            "Rank": "2",
                            "IRM": "",
                            "TotalTime": "25:20.0",
                        }
                    ],
                },
            ),
            (
                "R2",
                {
                    "Competition": {
                        "DisciplineId": "SR",
                        "catId": "MX",
                        "Description": "Single Mixed Relay",
                        "StartTime": "2026-02-02T10:00:00Z",
                    },
                    "Results": [
                        {"IsTeam": True, "Bib": "1", "Nat": "FRA", "Rank": "1"},
                        {
                            "IsTeam": False,
                            "IBUId": "A",
                            "Name": "Alpha",
                            "Nat": "FRA",
                            "Bib": "1",
                            "Leg": "1",
                            "Rank": "1",
                            "IRM": "",
                            "TotalTime": "34:00.0",
                        },
                    ],
                },
            ),
        ],
        all_results_cache={"A": []},
        race_start_cache={},
        output_format="tsv",
    )

    out = capsys.readouterr().out
    assert "#### Indiv Races" in out
    assert "#### Team Races" in out
    assert len(captured) == 2
    assert captured[0][1][0][2].startswith("Best career indiv")
    assert captured[1][1][0][2].startswith("Best career team")
    assert captured[0][1][0][6] == "R1"
    assert captured[1][1][0][6] == "R2"


def test_render_postevent_best_performances_includes_event_scope_milestones(
    monkeypatch,
):
    captured: list[tuple[list[str], list[list[str]], dict]] = []

    def fake_render_table(headers, rows, **kwargs):
        captured.append((headers, rows, kwargs))

    monkeypatch.setattr(brief, "render_table", fake_render_table)
    monkeypatch.setattr(brief, "_prefetch_bios", lambda ibu_ids: {})
    monkeypatch.setattr(brief, "_is_result_at_or_before_target", lambda *a, **k: True)

    brief._render_postevent_best_performances(
        argparse.Namespace(format="tsv"),
        completed_races=[
            (
                "R1",
                {
                    "Competition": {
                        "DisciplineId": "SP",
                        "catId": "SW",
                        "Description": "Sprint",
                        "StartTime": "2026-02-01T10:00:00Z",
                    },
                    "Results": [
                        {
                            "IsTeam": False,
                            "IBUId": "A",
                            "Name": "Alpha",
                            "Nat": "FRA",
                            "Rank": "2",
                            "IRM": "",
                            "TotalTime": "25:20.0",
                        }
                    ],
                },
            )
        ],
        all_results_cache={
            "A": [
                {
                    "Level": "WC",
                    "RaceId": "OLD_WC",
                    "DisciplineId": "SP",
                    "Rank": "1",
                },
                {
                    "Level": "OWG",
                    "RaceId": "OLD_OWG",
                    "DisciplineId": "IN",
                    "Rank": "5",
                },
            ]
        },
        race_start_cache={},
        output_format="tsv",
        event_type="OWG",
    )

    assert len(captured) == 1
    _headers, rows, _kwargs = captured[0]
    assert rows == [
        [
            "Alpha",
            "FRA",
            "Best Olympic Games indiv result (all discipline)",
            "2",
            "5th (all discipline, Olympic Games)",
            "Sprint",
            "R1",
        ]
    ]


def test_render_postevent_best_performances_group_sort_uses_delta_then_age_then_name(
    monkeypatch,
):
    captured: list[tuple[list[str], list[list[str]], dict]] = []

    def fake_render_table(headers, rows, **kwargs):
        captured.append((headers, rows, kwargs))

    monkeypatch.setattr(brief, "render_table", fake_render_table)
    monkeypatch.setattr(
        brief,
        "_prefetch_bios",
        lambda ibu_ids: {
            "A": {"BirthDate": "1995-01-01"},
            "B": {"BirthDate": "2002-01-01"},
            "C": {"BirthDate": "1998-01-01"},
        },
    )
    monkeypatch.setattr(brief, "_is_result_at_or_before_target", lambda *a, **k: True)

    brief._render_postevent_best_performances(
        argparse.Namespace(format="tsv"),
        completed_races=[
            (
                "R1",
                {
                    "Competition": {
                        "DisciplineId": "SP",
                        "catId": "SW",
                        "Description": "Sprint",
                        "StartTime": "2026-02-01T10:00:00Z",
                    },
                    "Results": [
                        {
                            "IsTeam": False,
                            "IBUId": "A",
                            "Name": "Zed",
                            "Nat": "NOR",
                            "Rank": "1",
                            "IRM": "",
                            "TotalTime": "25:00.0",
                        }
                    ],
                },
            ),
            (
                "R2",
                {
                    "Competition": {
                        "DisciplineId": "SP",
                        "catId": "SW",
                        "Description": "Sprint",
                        "StartTime": "2026-02-02T10:00:00Z",
                    },
                    "Results": [
                        {
                            "IsTeam": False,
                            "IBUId": "B",
                            "Name": "Zoe",
                            "Nat": "FRA",
                            "Rank": "1",
                            "IRM": "",
                            "TotalTime": "24:50.0",
                        }
                    ],
                },
            ),
            (
                "R3",
                {
                    "Competition": {
                        "DisciplineId": "SP",
                        "catId": "SW",
                        "Description": "Sprint",
                        "StartTime": "2026-02-03T10:00:00Z",
                    },
                    "Results": [
                        {
                            "IsTeam": False,
                            "IBUId": "C",
                            "Name": "Amy",
                            "Nat": "SWE",
                            "Rank": "1",
                            "IRM": "",
                            "TotalTime": "24:40.0",
                        }
                    ],
                },
            ),
        ],
        all_results_cache={
            "A": [
                {
                    "Level": "WC",
                    "RaceId": "OLD_A",
                    "DisciplineId": "SP",
                    "Rank": "3",
                }
            ],
            "B": [
                {
                    "Level": "WC",
                    "RaceId": "OLD_B",
                    "DisciplineId": "SP",
                    "Rank": "2",
                }
            ],
            "C": [
                {
                    "Level": "WC",
                    "RaceId": "OLD_C",
                    "DisciplineId": "SP",
                    "Rank": "2",
                }
            ],
        },
        race_start_cache={},
        output_format="tsv",
    )

    assert len(captured) == 1
    _headers, rows, _kwargs = captured[0]
    assert [row[0] for row in rows] == ["Zed", "Zoe", "Amy"]


def test_render_postevent_best_performances_skips_dns_status_rows(capsys):
    brief._render_postevent_best_performances(
        argparse.Namespace(format="tsv"),
        completed_races=[
            (
                "R1",
                {
                    "Competition": {
                        "DisciplineId": "SI",
                        "StartTime": "2026-02-01T10:00:00Z",
                    },
                    "Results": [
                        {
                            "IsTeam": False,
                            "IBUId": "10093",
                            "Name": "LANGEL Coralie",
                            "Nat": "SUI",
                            "Rank": "10093",
                            "IRM": "DNS",
                            "Result": "DNS",
                        }
                    ],
                },
            )
        ],
        all_results_cache={"10093": []},
        race_start_cache={},
        output_format="tsv",
    )

    out = capsys.readouterr().out
    assert "LANGEL Coralie" not in out
    assert "\nnone\n" in out


def test_render_postevent_best_performances_keeps_rank_25_max(monkeypatch):
    captured: list[tuple[list[str], list[list[str]], dict]] = []

    def fake_render_table(headers, rows, **kwargs):
        captured.append((headers, rows, kwargs))

    monkeypatch.setattr(brief, "render_table", fake_render_table)
    monkeypatch.setattr(brief, "_prefetch_bios", lambda ibu_ids: {})
    brief._render_postevent_best_performances(
        argparse.Namespace(format="tsv"),
        completed_races=[
            (
                "R1",
                {
                    "Competition": {
                        "DisciplineId": "SP",
                        "catId": "SW",
                        "Description": "Sprint",
                        "StartTime": "2026-02-01T10:00:00Z",
                    },
                    "Results": [
                        {
                            "IsTeam": False,
                            "IBUId": "A",
                            "Name": "Alpha",
                            "Nat": "FRA",
                            "Rank": "25",
                            "IRM": "",
                            "TotalTime": "26:00.0",
                        },
                        {
                            "IsTeam": False,
                            "IBUId": "B",
                            "Name": "Bravo",
                            "Nat": "GER",
                            "Rank": "26",
                            "IRM": "",
                            "TotalTime": "26:10.0",
                        },
                    ],
                },
            )
        ],
        all_results_cache={"A": [], "B": []},
        race_start_cache={},
        output_format="tsv",
    )

    assert len(captured) == 1
    rows = captured[0][1]
    assert [row[0] for row in rows] == ["Alpha"]
    assert all(int(row[3]) <= 25 for row in rows)


def test_build_postevent_event_milestone_rows_hits_race_and_win_thresholds(monkeypatch):
    monkeypatch.setattr(brief, "_is_result_at_or_before_target", lambda *a, **k: True)

    prior_results = []
    for idx in range(49):
        prior_results.append(
            {
                "Level": "WC",
                "RaceId": f"OLD{idx}",
                "DisciplineId": "SP",
                "Rank": "1" if idx < 9 else "2",
            }
        )

    race_rows, win_rows = brief._build_postevent_event_milestone_rows(
        completed_races=[
            (
                "R1",
                {
                    "Competition": {
                        "DisciplineId": "SP",
                        "catId": "SW",
                        "Description": "Sprint",
                        "StartTime": "2026-02-01T10:00:00Z",
                    },
                    "Results": [
                        {
                            "IsTeam": False,
                            "IBUId": "A",
                            "Name": "Alpha",
                            "Nat": "FRA",
                            "Rank": "1",
                            "IRM": "",
                            "TotalTime": "25:00.0",
                        }
                    ],
                },
            )
        ],
        all_results_cache={"A": prior_results},
        race_start_cache={},
        event_type="WC",
    )

    assert race_rows == [
        ("WC", "F", 50, "Race", "Alpha", "FRA", "Sprint", "R1"),
        ("WC", "F", 50, "Indiv Race", "Alpha", "FRA", "Sprint", "R1"),
        ("WC+WCH+OWG", "F", 50, "Race", "Alpha", "FRA", "Sprint", "R1"),
        ("WC+WCH+OWG", "F", 50, "Indiv Race", "Alpha", "FRA", "Sprint", "R1"),
    ]
    assert win_rows == [
        ("WC", 10, "Win", "Alpha", "FRA", "Sprint", "R1"),
        ("WC", 10, "Indiv Win", "Alpha", "FRA", "Sprint", "R1"),
        ("WC+WCH+OWG", 10, "Win", "Alpha", "FRA", "Sprint", "R1"),
        ("WC+WCH+OWG", 10, "Indiv Win", "Alpha", "FRA", "Sprint", "R1"),
    ]


def test_build_postevent_event_milestone_rows_dedupes_single_mixed_athletes(
    monkeypatch,
):
    monkeypatch.setattr(brief, "_is_result_at_or_before_target", lambda *a, **k: True)

    prior_results = []
    for idx in range(49):
        prior_results.append(
            {
                "Level": "WC",
                "RaceId": f"OLD{idx}",
                "DisciplineId": "SR",
                "Rank": "1" if idx < 9 else "2",
            }
        )

    race_rows, win_rows = brief._build_postevent_event_milestone_rows(
        completed_races=[
            (
                "RSM",
                {
                    "Competition": {
                        "DisciplineId": "SR",
                        "catId": "MX",
                        "Description": "Single Mixed Relay",
                        "StartTime": "2026-02-01T12:00:00Z",
                    },
                    "Results": [
                        {"IsTeam": True, "Bib": "1", "Nat": "FRA", "Rank": "1"},
                        {
                            "IsTeam": False,
                            "IBUId": "A",
                            "Name": "Alpha",
                            "Nat": "FRA",
                            "Bib": "1",
                            "Leg": "1",
                            "Rank": "1",
                            "IRM": "",
                            "TotalTime": "34:00.0",
                        },
                        {
                            "IsTeam": False,
                            "IBUId": "A",
                            "Name": "Alpha",
                            "Nat": "FRA",
                            "Bib": "1",
                            "Leg": "3",
                            "Rank": "1",
                            "IRM": "",
                            "TotalTime": "34:00.0",
                        },
                    ],
                },
            )
        ],
        all_results_cache={"A": prior_results},
        race_start_cache={},
        event_type="WC",
    )

    assert len(race_rows) == 4
    assert len(win_rows) == 4
    assert (
        "WC",
        "F",
        50,
        "Race",
        "Alpha",
        "FRA",
        "Single Mixed Relay",
        "RSM",
    ) in race_rows
    assert (
        "WC",
        "F",
        50,
        "Team Race",
        "Alpha",
        "FRA",
        "Single Mixed Relay",
        "RSM",
    ) in race_rows
    assert (
        "WC+WCH+OWG",
        "F",
        50,
        "Race",
        "Alpha",
        "FRA",
        "Single Mixed Relay",
        "RSM",
    ) in race_rows
    assert (
        "WC+WCH+OWG",
        "F",
        50,
        "Team Race",
        "Alpha",
        "FRA",
        "Single Mixed Relay",
        "RSM",
    ) in race_rows
    assert (
        "WC",
        10,
        "Win",
        "Alpha",
        "FRA",
        "Single Mixed Relay",
        "RSM",
    ) in win_rows
    assert (
        "WC",
        10,
        "Relay Win",
        "Alpha",
        "FRA",
        "Single Mixed Relay",
        "RSM",
    ) in win_rows
    assert (
        "WC+WCH+OWG",
        10,
        "Win",
        "Alpha",
        "FRA",
        "Single Mixed Relay",
        "RSM",
    ) in win_rows
    assert (
        "WC+WCH+OWG",
        10,
        "Relay Win",
        "Alpha",
        "FRA",
        "Single Mixed Relay",
        "RSM",
    ) in win_rows


def test_build_postevent_event_milestone_rows_uses_career_win_rule_for_career_scope(
    monkeypatch,
):
    monkeypatch.setattr(brief, "_is_result_at_or_before_target", lambda *a, **k: True)

    prior_results = []
    for idx in range(4):
        prior_results.append(
            {
                "Level": "WC",
                "RaceId": f"OLD{idx}",
                "DisciplineId": "SP",
                "Rank": "1",
            }
        )

    _race_rows, win_rows = brief._build_postevent_event_milestone_rows(
        completed_races=[
            (
                "R1",
                {
                    "Competition": {
                        "DisciplineId": "SP",
                        "catId": "SW",
                        "Description": "Sprint",
                        "StartTime": "2026-02-01T10:00:00Z",
                    },
                    "Results": [
                        {
                            "IsTeam": False,
                            "IBUId": "A",
                            "Name": "Alpha",
                            "Nat": "FRA",
                            "Rank": "1",
                            "IRM": "",
                            "TotalTime": "25:00.0",
                        }
                    ],
                },
            )
        ],
        all_results_cache={"A": prior_results},
        race_start_cache={},
        event_type="WC",
    )

    assert win_rows == [
        ("WC+WCH+OWG", 5, "Win", "Alpha", "FRA", "Sprint", "R1"),
        ("WC+WCH+OWG", 5, "Indiv Win", "Alpha", "FRA", "Sprint", "R1"),
    ]


def test_render_postevent_race_milestone_section_splits_event_and_career(monkeypatch):
    captured: list[tuple[list[str], list[list[str]], dict]] = []

    def fake_render_table(headers, rows, **kwargs):
        captured.append((headers, rows, kwargs))

    monkeypatch.setattr(brief, "render_table", fake_render_table)
    brief._render_postevent_race_milestone_section(
        argparse.Namespace(format="tsv"),
        rows=[
            ("WC", "F", 50, "Race", "Alpha", "FRA", "Sprint", "R1"),
            ("WC+WCH+OWG", "F", 100, "Race", "Alpha", "FRA", "Sprint", "R1"),
        ],
        output_format="tsv",
        event_scope_label="WC",
    )

    assert len(captured) == 2
    assert captured[0][1][0][0] == "50th"
    assert captured[1][1][0][0] == "100th"
    assert captured[0][1][0][5] == "R1"
    assert captured[1][1][0][5] == "R1"
    assert captured[0][2].get("row_separators") is None
    assert captured[1][2].get("row_separators") is None


def test_render_postevent_race_milestone_section_adds_blank_lines_between_headers(
    monkeypatch, capsys
):
    monkeypatch.setattr(
        brief, "render_table", lambda headers, rows, **kwargs: print("TABLE")
    )

    brief._render_postevent_race_milestone_section(
        argparse.Namespace(format="tsv"),
        rows=[("WC", "F", 50, "Race", "Alpha", "FRA", "Sprint", "R1")],
        output_format="tsv",
        event_scope_label="WC",
    )

    out = capsys.readouterr().out
    assert out.startswith("### WC\n\n#### Women\n\nTABLE\n\n\n#### Men\n\nnone\n\n\n")
    assert "\n### WC+WCH+OWG\n\n#### Women\n\nnone\n\n\n#### Men\n\nnone\n\n\n" in out


def test_render_postevent_race_milestone_section_groups_rows_by_athlete(monkeypatch):
    captured: list[tuple[list[str], list[list[str]], dict]] = []

    def fake_render_table(headers, rows, **kwargs):
        captured.append((headers, rows, kwargs))

    monkeypatch.setattr(brief, "render_table", fake_render_table)
    brief._render_postevent_race_milestone_section(
        argparse.Namespace(format="tsv"),
        rows=[
            ("WC", "F", 100, "Race", "Alpha", "FRA", "Sprint", "R1"),
            ("WC", "F", 50, "Race", "Alpha", "FRA", "Pursuit", "R2"),
            ("WC", "F", 75, "Race", "Bravo", "NOR", "Sprint", "R3"),
        ],
        output_format="tsv",
        event_scope_label="WC",
    )

    assert len(captured) == 1
    assert [row[2] for row in captured[0][1]] == ["Alpha", "Alpha", "Bravo"]
    assert captured[0][2].get("row_separators") is None


def test_render_postevent_race_milestone_section_splits_women_and_men(monkeypatch):
    captured: list[tuple[list[str], list[list[str]], dict]] = []

    def fake_render_table(headers, rows, **kwargs):
        captured.append((headers, rows, kwargs))

    monkeypatch.setattr(brief, "render_table", fake_render_table)
    brief._render_postevent_race_milestone_section(
        argparse.Namespace(format="tsv"),
        rows=[
            ("WC", "F", 50, "Race", "Alpha", "FRA", "Sprint", "R1"),
            ("WC", "M", 50, "Race", "Bravo", "NOR", "Sprint", "R2"),
        ],
        output_format="tsv",
        event_scope_label="WC",
    )

    assert len(captured) == 2
    assert captured[0][1][0][2] == "Alpha"
    assert captured[1][1][0][2] == "Bravo"


def test_render_postevent_race_milestone_section_limits_rows_per_type(monkeypatch):
    captured: list[tuple[list[str], list[list[str]], dict]] = []

    def fake_render_table(headers, rows, **kwargs):
        captured.append((headers, rows, kwargs))

    monkeypatch.setattr(brief, "render_table", fake_render_table)
    brief._render_postevent_race_milestone_section(
        argparse.Namespace(format="tsv"),
        rows=[
            ("WC", "F", 25, "Race", "Race A", "FRA", "Sprint", "R1"),
            ("WC", "F", 20, "Race", "Race B", "NOR", "Sprint", "R2"),
            ("WC", "F", 15, "Race", "Race C", "SWE", "Sprint", "R3"),
            ("WC", "F", 10, "Race", "Race D", "GER", "Sprint", "R4"),
            ("WC", "F", 21, "Race", "Race X", "AUT", "Sprint", "R5"),
            ("WC", "F", 30, "Indiv Race", "Indiv A", "FRA", "Pursuit", "R6"),
            ("WC", "F", 25, "Indiv Race", "Indiv B", "NOR", "Pursuit", "R7"),
            ("WC", "F", 20, "Indiv Race", "Indiv C", "SWE", "Pursuit", "R8"),
            ("WC", "F", 15, "Indiv Race", "Indiv D", "GER", "Pursuit", "R9"),
            ("WC", "F", 8, "Team Race", "Team A", "FRA", "Relay", "R10"),
            ("WC", "F", 6, "Team Race", "Team B", "NOR", "Relay", "R11"),
            ("WC", "F", 4, "Team Race", "Team C", "SWE", "Relay", "R12"),
            ("WC", "F", 2, "Team Race", "Team D", "GER", "Relay", "R13"),
            ("WC", "F", 7, "Team Race", "Team X", "AUT", "Relay", "R14"),
        ],
        output_format="tsv",
        event_scope_label="WC",
    )

    assert len(captured) == 1
    table_rows = captured[0][1]
    by_type: dict[str, set[str]] = {}
    for row in table_rows:
        by_type.setdefault(row[1], set()).add(row[2])

    assert by_type["Race"] == {"Race A", "Race B", "Race C"}
    assert by_type["Indiv Race"] == {"Indiv A", "Indiv B", "Indiv C"}
    assert by_type["Team Race"] == {"Team A", "Team B", "Team C"}
    assert all(
        row[2] not in {"Race D", "Race X", "Indiv D", "Team D", "Team X"}
        for row in table_rows
    )
    assert captured[0][2].get("row_separators") is None


def test_render_postevent_win_milestone_section_splits_event_and_career(monkeypatch):
    captured: list[tuple[list[str], list[list[str]], dict]] = []

    def fake_render_table(headers, rows, **kwargs):
        captured.append((headers, rows, kwargs))

    monkeypatch.setattr(brief, "render_table", fake_render_table)
    brief._render_postevent_win_milestone_section(
        argparse.Namespace(format="tsv"),
        rows=[
            ("WC", 5, "Win", "Alpha", "FRA", "Sprint", "R1"),
            ("WC+WCH+OWG", 10, "Win", "Alpha", "FRA", "Sprint", "R1"),
        ],
        output_format="tsv",
        event_scope_label="WC",
    )

    assert len(captured) == 2
    assert captured[0][1][0][0] == "5th"
    assert captured[1][1][0][0] == "10th"
    assert captured[0][1][0][5] == "R1"
    assert captured[1][1][0][5] == "R1"
    assert captured[0][2].get("row_separators") is None
    assert captured[1][2].get("row_separators") is None


def test_render_postevent_win_milestone_section_groups_rows_by_athlete(monkeypatch):
    captured: list[tuple[list[str], list[list[str]], dict]] = []

    def fake_render_table(headers, rows, **kwargs):
        captured.append((headers, rows, kwargs))

    monkeypatch.setattr(brief, "render_table", fake_render_table)
    brief._render_postevent_win_milestone_section(
        argparse.Namespace(format="tsv"),
        rows=[
            ("WC", 10, "Win", "Alpha", "FRA", "Sprint", "R1"),
            ("WC", 5, "Indiv Win", "Alpha", "FRA", "Pursuit", "R2"),
            ("WC", 8, "Win", "Bravo", "NOR", "Sprint", "R3"),
        ],
        output_format="tsv",
        event_scope_label="WC",
    )

    assert len(captured) == 1
    assert [row[2] for row in captured[0][1]] == ["Alpha", "Alpha", "Bravo"]
    assert captured[0][2].get("row_separators") is None


def test_race_milestone_hit_rules_follow_event_type():
    assert brief._is_race_milestone_hit("WC", 1)
    assert brief._is_race_milestone_hit("WC", 50)
    assert not brief._is_race_milestone_hit("WC", 10)

    assert not brief._is_race_milestone_hit("WCH", 1)
    assert brief._is_race_milestone_hit("WCH", 5)
    assert brief._is_race_milestone_hit("WCH", 10)

    assert not brief._is_race_milestone_hit("OWG", 1)
    assert not brief._is_race_milestone_hit("OWG", 2)
    assert not brief._is_race_milestone_hit("OWG", 3)
    assert not brief._is_race_milestone_hit("OWG", 4)
    assert not brief._is_race_milestone_hit("OWG", 5)
    assert brief._is_race_milestone_hit("OWG", 15)
    assert brief._is_race_milestone_hit("OWG", 10)
    assert brief._is_race_milestone_hit("OWG", 20)
    assert not brief._is_race_milestone_hit("OWG", 21)


def test_class_race_milestone_hit_rules_follow_event_type():
    assert brief._is_class_race_milestone_hit("WC", 1)
    assert brief._is_class_race_milestone_hit("WC", 50)
    assert not brief._is_class_race_milestone_hit("WC", 10)

    assert not brief._is_class_race_milestone_hit("WCH", 1)
    assert brief._is_class_race_milestone_hit("WCH", 5)
    assert brief._is_class_race_milestone_hit("WCH", 10)

    assert not brief._is_class_race_milestone_hit("OWG", 1)
    assert not brief._is_class_race_milestone_hit("OWG", 2)
    assert not brief._is_class_race_milestone_hit("OWG", 3)
    assert not brief._is_class_race_milestone_hit("OWG", 4)
    assert brief._is_class_race_milestone_hit("OWG", 5)
    assert brief._is_class_race_milestone_hit("OWG", 10)
    assert brief._is_class_race_milestone_hit("OWG", 15)
    assert brief._is_class_race_milestone_hit("OWG", 20)
    assert not brief._is_class_race_milestone_hit("OWG", 21)


def test_build_postevent_event_milestone_rows_owg_keeps_fifth_for_class_only(
    monkeypatch,
):
    monkeypatch.setattr(brief, "_is_result_at_or_before_target", lambda *a, **k: True)

    prior_results = []
    for idx in range(4):
        prior_results.append(
            {
                "Level": "OWG",
                "RaceId": f"OLD{idx}",
                "DisciplineId": "SP",
                "Rank": "25",
            }
        )

    race_rows, win_rows = brief._build_postevent_event_milestone_rows(
        completed_races=[
            (
                "R1",
                {
                    "Competition": {
                        "DisciplineId": "SP",
                        "catId": "SW",
                        "Description": "Sprint",
                        "StartTime": "2026-02-01T10:00:00Z",
                    },
                    "Results": [
                        {
                            "IsTeam": False,
                            "IBUId": "A",
                            "Name": "Alpha",
                            "Nat": "FRA",
                            "Rank": "2",
                            "IRM": "",
                            "TotalTime": "25:00.0",
                        }
                    ],
                },
            )
        ],
        all_results_cache={"A": prior_results},
        race_start_cache={},
        event_type="OWG",
    )

    assert (
        "OWG",
        "F",
        5,
        "Indiv Race",
        "Alpha",
        "FRA",
        "Sprint",
        "R1",
    ) in race_rows
    assert (
        "OWG",
        "F",
        5,
        "Race",
        "Alpha",
        "FRA",
        "Sprint",
        "R1",
    ) not in race_rows
    assert win_rows == []


def test_career_race_milestone_hit_rules():
    assert brief._is_career_race_milestone_hit(1)
    assert brief._is_career_race_milestone_hit(25)
    assert brief._is_career_race_milestone_hit(50)
    assert brief._is_career_race_milestone_hit(75)
    assert brief._is_career_race_milestone_hit(100)
    assert not brief._is_career_race_milestone_hit(10)
    assert not brief._is_career_race_milestone_hit(11)
    assert not brief._is_career_race_milestone_hit(26)


def test_win_milestone_hit_rules_follow_event_type():
    assert brief._is_win_milestone_hit("WC", 1)
    assert brief._is_win_milestone_hit("WC", 10)
    assert brief._is_win_milestone_hit("WC", 25)
    assert not brief._is_win_milestone_hit("WC", 5)

    assert brief._is_win_milestone_hit("WCH", 2)
    assert brief._is_win_milestone_hit("WCH", 7)
    assert brief._is_win_milestone_hit("OWG", 2)
    assert brief._is_win_milestone_hit("OWG", 7)


def test_career_win_milestone_hit_rules():
    assert brief._is_career_win_milestone_hit(1)
    assert brief._is_career_win_milestone_hit(5)
    assert brief._is_career_win_milestone_hit(10)
    assert not brief._is_career_win_milestone_hit(2)
    assert not brief._is_career_win_milestone_hit(11)


def test_render_postevent_athlete_standings_adds_all_leader_markers(monkeypatch):
    captured: list[tuple[list[str], list[list[str]], dict]] = []

    def fake_render_table(headers, rows, **kwargs):
        captured.append((headers, rows, kwargs))

    monkeypatch.setattr(brief, "render_table", fake_render_table)
    brief._render_postevent_athlete_standings(
        argparse.Namespace(format="pretty"),
        before_standings={
            "athlete": {"SW": {"TS": [], "SP": []}, "SM": {"TS": [], "SP": []}},
            "relay": {},
            "nations": {},
        },
        after_standings={
            "athlete": {
                "SW": {
                    "TS": [
                        {
                            "Rank": "1",
                            "IBUId": "A",
                            "Name": "Alpha",
                            "Nat": "FRA",
                            "Score": "100",
                            "BestU23": 1,
                        }
                    ],
                    "SP": [
                        {
                            "Rank": "1",
                            "IBUId": "A",
                            "Name": "Alpha",
                            "Nat": "FRA",
                            "Score": "50",
                            "BestU23": 1,
                        }
                    ],
                },
                "SM": {"TS": [], "SP": []},
            },
            "relay": {},
            "nations": {},
        },
        disciplines_raced={("SP", "SW")},
    )

    assert captured
    name_cell = captured[0][1][0][1]
    assert brief.GENERAL_LEADER_MARKER in name_cell
    assert brief.DISCIPLINE_LEADER_MARKER in name_cell
    assert brief.U23_LEADER_MARKER in name_cell


def test_render_postevent_athlete_standings_infers_u23_from_age(monkeypatch):
    captured: list[tuple[list[str], list[list[str]], dict]] = []

    def fake_render_table(headers, rows, **kwargs):
        captured.append((headers, rows, kwargs))

    monkeypatch.setattr(brief, "render_table", fake_render_table)
    monkeypatch.setattr(
        brief,
        "_prefetch_bios",
        lambda ibu_ids: {
            "A": {"BirthDate": "1998-05-10"},
            "B": {"BirthDate": "2003-06-01"},
        },
    )
    brief._render_postevent_athlete_standings(
        argparse.Namespace(format="pretty"),
        before_standings={
            "athlete": {"SW": {"TS": []}, "SM": {"TS": []}},
            "relay": {},
            "nations": {},
        },
        after_standings={
            "athlete": {
                "SW": {
                    "TS": [
                        {
                            "Rank": "1",
                            "IBUId": "A",
                            "Name": "Alpha",
                            "Nat": "FRA",
                            "Score": "100",
                        },
                        {
                            "Rank": "2",
                            "IBUId": "B",
                            "Name": "Bravo",
                            "Nat": "GER",
                            "Score": "90",
                        },
                    ]
                },
                "SM": {"TS": []},
            },
            "relay": {},
            "nations": {},
        },
        disciplines_raced=set(),
        season_id="2526",
    )

    assert captured
    rows = captured[0][1]
    bravo_name = next(row[1] for row in rows if row[1].startswith("Bravo"))
    assert brief.U23_LEADER_MARKER in bravo_name


def test_build_postevent_decorated_delta_rows_appends_value_delta_flags():
    before_rows = [
        [
            "1",
            "Alpha",
            "NOR",
            "F",
            "2",
            "0",
            "0",
            "2",
            "9",
            "2",
            "0",
            "0",
            "2",
            "7",
            "0",
            "0",
            "0",
            "0",
            "2",
        ],
        [
            "2",
            "Bravo",
            "FRA",
            "F",
            "1",
            "1",
            "0",
            "2",
            "8",
            "0",
            "1",
            "0",
            "1",
            "5",
            "1",
            "0",
            "0",
            "1",
            "3",
        ],
    ]
    after_rows = [
        [
            "1",
            "Bravo",
            "FRA",
            "F",
            "2",
            "1",
            "1",
            "4",
            "12",
            "1",
            "1",
            "1",
            "3",
            "8",
            "1",
            "0",
            "0",
            "1",
            "4",
        ],
        [
            "2",
            "Alpha",
            "NOR",
            "F",
            "2",
            "0",
            "0",
            "2",
            "10",
            "2",
            "0",
            "0",
            "2",
            "8",
            "0",
            "0",
            "0",
            "0",
            "2",
        ],
        [
            "3",
            "Charlie",
            "GER",
            "F",
            "1",
            "0",
            "0",
            "1",
            "3",
            "1",
            "0",
            "0",
            "1",
            "2",
            "0",
            "0",
            "0",
            "0",
            "1",
        ],
    ]

    rows, styles = brief._build_postevent_decorated_delta_rows(
        after_rows, before_rows, "F", limit=3
    )

    assert rows[0] == [
        "1",
        "Bravo",
        "FRA",
        "2 (+1)",
        "1",
        "1 (+1)",
        "4 (+2)",
        "12",
        "1 (+1)",
        "1",
        "1 (+1)",
        "3 (+2)",
        "8",
        "1",
        "0",
        "0",
        "1",
        "4",
    ]
    assert rows[1] == [
        "2",
        "Alpha",
        "NOR",
        "2",
        "0",
        "0",
        "2",
        "10",
        "2",
        "0",
        "0",
        "2",
        "8",
        "0",
        "0",
        "0",
        "0",
        "2",
    ]
    assert rows[2] == [
        "3",
        "Charlie",
        "GER",
        "1 (+1)",
        "0",
        "0",
        "1 (+1)",
        "3",
        "1 (+1)",
        "0",
        "0",
        "1 (+1)",
        "2",
        "0",
        "0",
        "0",
        "0",
        "1",
    ]
    assert styles == ["", "", ""]


def test_build_postevent_decorated_delta_rows_dims_non_participants_when_available():
    before_rows = []
    after_rows = [
        [
            "1",
            "Current Athlete",
            "FRA",
            "F",
            "2",
            "0",
            "0",
            "2",
            "5",
            "2",
            "0",
            "0",
            "2",
            "5",
            "0",
            "0",
            "0",
            "0",
            "0",
        ],
        [
            "2",
            "Past Athlete",
            "GER",
            "F",
            "1",
            "1",
            "0",
            "2",
            "9",
            "1",
            "1",
            "0",
            "2",
            "9",
            "0",
            "0",
            "0",
            "0",
            "0",
        ],
    ]

    _rows, styles = brief._build_postevent_decorated_delta_rows(
        after_rows,
        before_rows,
        "F",
        limit=2,
        after_row_styles=["highlight_plain", ""],
    )

    assert styles == ["highlight_plain", "dim"]


def test_build_postevent_decorated_delta_rows_matches_name_order_variants():
    before_rows = [
        [
            "1",
            "SIMON Julia",
            "FRA",
            "F",
            "2",
            "1",
            "0",
            "3",
            "7",
            "2",
            "1",
            "0",
            "3",
            "6",
            "0",
            "0",
            "0",
            "0",
            "1",
        ]
    ]
    after_rows = [
        [
            "1",
            "Julia SIMON",
            "FRA",
            "F",
            "2",
            "1",
            "0",
            "3",
            "7",
            "2",
            "1",
            "0",
            "3",
            "6",
            "0",
            "0",
            "0",
            "0",
            "1",
        ]
    ]

    rows, _styles = brief._build_postevent_decorated_delta_rows(
        after_rows, before_rows, "F", limit=1
    )

    assert rows[0] == [
        "1",
        "Julia SIMON",
        "FRA",
        "2",
        "1",
        "0",
        "3",
        "7",
        "2",
        "1",
        "0",
        "3",
        "6",
        "0",
        "0",
        "0",
        "0",
        "1",
    ]


def test_build_postevent_decorated_delta_rows_uses_best_previous_duplicate():
    before_rows = [
        [
            "1",
            "ROETSCH Frank Peter",
            "GER",
            "M",
            "2",
            "1",
            "0",
            "3",
            "7",
            "2",
            "1",
            "0",
            "3",
            "6",
            "0",
            "0",
            "0",
            "0",
            "1",
        ],
        [
            "20",
            "Frank Peter ROETSCH",
            "GER",
            "M",
            "0",
            "0",
            "0",
            "0",
            "1",
            "0",
            "0",
            "0",
            "0",
            "1",
            "0",
            "0",
            "0",
            "0",
            "0",
        ],
    ]
    after_rows = [
        [
            "12",
            "ROETSCH Frank Peter",
            "GER",
            "M",
            "2",
            "1",
            "0",
            "3",
            "7",
            "2",
            "1",
            "0",
            "3",
            "6",
            "0",
            "0",
            "0",
            "0",
            "1",
        ]
    ]

    rows, _styles = brief._build_postevent_decorated_delta_rows(
        after_rows, before_rows, "M", limit=15
    )

    assert rows[0] == [
        "1",
        "ROETSCH Frank Peter",
        "GER",
        "2",
        "1",
        "0",
        "3",
        "7",
        "2",
        "1",
        "0",
        "3",
        "6",
        "0",
        "0",
        "0",
        "0",
        "1",
    ]


def test_build_postevent_decorated_delta_rows_falls_back_when_nat_changes():
    before_rows = [
        [
            "1",
            "ROETSCH Frank Peter",
            "FRG",
            "M",
            "2",
            "1",
            "0",
            "3",
            "7",
            "2",
            "1",
            "0",
            "3",
            "6",
            "0",
            "0",
            "0",
            "0",
            "1",
        ]
    ]
    after_rows = [
        [
            "1",
            "ROETSCH Frank Peter",
            "GER",
            "M",
            "2",
            "1",
            "0",
            "3",
            "7",
            "2",
            "1",
            "0",
            "3",
            "6",
            "0",
            "0",
            "0",
            "0",
            "1",
        ]
    ]

    rows, _styles = brief._build_postevent_decorated_delta_rows(
        after_rows, before_rows, "M", limit=10
    )

    assert rows[0] == [
        "1",
        "ROETSCH Frank Peter",
        "GER",
        "2",
        "1",
        "0",
        "3",
        "7",
        "2",
        "1",
        "0",
        "3",
        "6",
        "0",
        "0",
        "0",
        "0",
        "1",
    ]


def test_build_postevent_decorated_delta_rows_keeps_top10_plus_new_winners():
    before_rows = []
    after_rows = []
    for rank in range(1, 13):
        gold = 1
        if rank == 11:
            before_gold = 0
        else:
            before_gold = 1
        before_rows.append(
            [
                str(rank),
                f"Athlete {rank}",
                "GER",
                "M",
                str(before_gold),
                "0",
                "0",
                str(before_gold),
                "1",
                str(before_gold),
                "0",
                "0",
                str(before_gold),
                "1",
                "0",
                "0",
                "0",
                "0",
                "0",
            ]
        )
        after_rows.append(
            [
                str(rank),
                f"Athlete {rank}",
                "GER",
                "M",
                str(gold),
                "0",
                "0",
                str(gold),
                "1",
                str(gold),
                "0",
                "0",
                str(gold),
                "1",
                "0",
                "0",
                "0",
                "0",
                "0",
            ]
        )

    rows, _styles = brief._build_postevent_decorated_delta_rows(
        after_rows, before_rows, "M", limit=10
    )

    assert len(rows) == 11
    assert rows[-1][0] == "11"
    assert rows[-1][1] == "Athlete 11"
    assert rows[-1][3] == "1 (+1)"
    assert all(row[1] != "Athlete 12" for row in rows)


def test_build_postevent_decorated_delta_rows_does_not_append_silver_only_changes():
    before_rows = []
    after_rows = []
    for rank in range(1, 13):
        before_gold = 1
        before_silver = 0
        after_gold = 1
        after_silver = 0
        if rank == 11:
            after_silver = 1
        before_total = before_gold + before_silver
        after_total = after_gold + after_silver
        before_rows.append(
            [
                str(rank),
                f"Athlete {rank}",
                "GER",
                "M",
                str(before_gold),
                str(before_silver),
                "0",
                str(before_total),
                "1",
                str(before_gold),
                str(before_silver),
                "0",
                str(before_total),
                "1",
                "0",
                "0",
                "0",
                "0",
                "0",
            ]
        )
        after_rows.append(
            [
                str(rank),
                f"Athlete {rank}",
                "GER",
                "M",
                str(after_gold),
                str(after_silver),
                "0",
                str(after_total),
                "1",
                str(after_gold),
                str(after_silver),
                "0",
                str(after_total),
                "1",
                "0",
                "0",
                "0",
                "0",
                "0",
            ]
        )

    rows, _styles = brief._build_postevent_decorated_delta_rows(
        after_rows, before_rows, "M", limit=10
    )

    assert len(rows) == 10
    assert all(row[1] != "Athlete 11" for row in rows)
