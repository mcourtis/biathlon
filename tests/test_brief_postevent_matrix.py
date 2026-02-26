"""Matrix and delta-row contract tests for brief postevent."""

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

    assert rows[0] == ["1", "Bravo", "FRA", "130", "2", "+1", "90", "+40"]
    assert rows[1] == ["2", "Charlie", "GER", "95", "-", "new", "-", "-"]
    assert rows[2] == ["3", "Alpha", "NOR", "100", "1", "-2", "100", "0"]
    assert styles == ["highlight_plain", "highlight_plain", "highlight_plain"]


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

    assert rows[0] == ["1", "France", "280", "2", "+1", "200", "+80"]
    assert rows[1] == ["2", "Norway", "260.5", "1", "-1", "250.5", "+10"]
    assert styles == ["highlight_plain", "highlight_plain"]


def test_build_postevent_decorated_delta_rows_reports_rank_and_medal_changes():
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
            "0",
            "0",
            "0",
            "0",
            "0",
            "0",
            "0",
            "0",
            "0",
            "0",
            "0",
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
            "0",
            "0",
            "0",
            "0",
            "0",
            "0",
            "0",
            "0",
            "0",
            "0",
            "0",
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
            "0",
            "3",
            "0",
            "0",
            "0",
            "0",
            "0",
            "0",
            "0",
            "0",
            "0",
            "0",
            "0",
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
            "0",
            "0",
            "0",
            "0",
            "0",
            "0",
            "0",
            "0",
            "0",
            "0",
            "0",
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
            "0",
            "0",
            "0",
            "0",
            "0",
            "0",
            "0",
            "0",
            "0",
            "0",
            "0",
        ],
    ]

    rows, styles = brief._build_postevent_decorated_delta_rows(
        after_rows, before_rows, "F", limit=3
    )

    assert rows[0] == [
        "1",
        "Bravo",
        "FRA",
        "2",
        "1",
        "0",
        "3",
        "2",
        "+1",
        "+1",
        "0",
        "0",
        "+1",
    ]
    assert rows[1] == [
        "2",
        "Alpha",
        "NOR",
        "2",
        "0",
        "0",
        "2",
        "1",
        "-1",
        "0",
        "0",
        "0",
        "0",
    ]
    assert rows[2] == [
        "3",
        "Charlie",
        "GER",
        "1",
        "0",
        "0",
        "1",
        "-",
        "new",
        "-",
        "-",
        "-",
        "-",
    ]
    assert styles == ["highlight_plain", "highlight_plain", "highlight_plain"]
