"""Tests for shared helpers in commands._common."""

import builtins

from biathlon.commands._common import (
    _max_workers,
    _ordinal,
    _parse_rank,
    _race_display_name,
    _row_ibu_id,
    _select_race_interactive,
    counts_toward_wc_standings,
    is_mixed_relay,
    is_relay_discipline,
)


class TestRowIbuId:
    def test_ibu_id_key(self):
        assert _row_ibu_id({"IBUId": "BTFRA12345"}) == "BTFRA12345"

    def test_ibu_id_alt_key(self):
        assert _row_ibu_id({"IbuId": "BTGER67890"}) == "BTGER67890"

    def test_ibu_id_lowercase_key(self):
        assert _row_ibu_id({"ibuId": "BTNOR11111"}) == "BTNOR11111"

    def test_id_fallback(self):
        assert _row_ibu_id({"Id": "BTITA22222"}) == "BTITA22222"

    def test_priority_order(self):
        assert _row_ibu_id({"IBUId": "A", "Id": "B"}) == "A"

    def test_empty_row(self):
        assert _row_ibu_id({}) == ""

    def test_none_value_skipped(self):
        assert _row_ibu_id({"IBUId": None, "Id": "X"}) == "X"

    def test_empty_string_skipped(self):
        assert _row_ibu_id({"IBUId": "", "IbuId": "Y"}) == "Y"


class TestParseRank:
    def test_integer_string(self):
        assert _parse_rank("1") == 1

    def test_trailing_dot(self):
        assert _parse_rank("3.") == 3

    def test_whitespace(self):
        assert _parse_rank(" 10 ") == 10

    def test_non_numeric(self):
        assert _parse_rank("DNF") is None

    def test_empty_string(self):
        assert _parse_rank("") is None

    def test_none(self):
        assert _parse_rank(None) is None

    def test_integer_input(self):
        assert _parse_rank(5) == 5


class TestOrdinal:
    def test_first(self):
        assert _ordinal(1) == "1st"

    def test_second(self):
        assert _ordinal(2) == "2nd"

    def test_third(self):
        assert _ordinal(3) == "3rd"

    def test_fourth(self):
        assert _ordinal(4) == "4th"

    def test_eleventh(self):
        assert _ordinal(11) == "11th"

    def test_twelfth(self):
        assert _ordinal(12) == "12th"

    def test_thirteenth(self):
        assert _ordinal(13) == "13th"

    def test_twenty_first(self):
        assert _ordinal(21) == "21st"

    def test_hundred_eleventh(self):
        assert _ordinal(111) == "111th"

    def test_hundred_first(self):
        assert _ordinal(101) == "101st"

    def test_twenty_fifth(self):
        assert _ordinal(25) == "25th"


class TestMaxWorkers:
    def test_default_cap(self):
        assert _max_workers(100) == 15

    def test_below_cap(self):
        assert _max_workers(5) == 5

    def test_custom_cap(self):
        assert _max_workers(100, cap=8) == 8

    def test_custom_cap_below(self):
        assert _max_workers(3, cap=8) == 3

    def test_zero_total(self):
        assert _max_workers(0) == 1

    def test_negative_total(self):
        assert _max_workers(-5) == 1


class TestIsRelayDiscipline:
    def test_relay(self):
        assert is_relay_discipline("RL") is True

    def test_legacy_team(self):
        assert is_relay_discipline("TM") is True

    def test_single_mixed(self):
        assert is_relay_discipline("SR") is True

    def test_mixed_relay(self):
        assert is_relay_discipline("MR") is True

    def test_sprint(self):
        assert is_relay_discipline("SP") is False

    def test_empty(self):
        assert is_relay_discipline("") is False


class TestIsMixedRelay:
    def test_mr(self):
        assert is_mixed_relay("MR", "MX") is True

    def test_sr(self):
        assert is_mixed_relay("SR", "MX") is True

    def test_rl_mixed_cat(self):
        assert is_mixed_relay("RL", "MX") is True

    def test_rl_women(self):
        assert is_mixed_relay("RL", "SW") is False

    def test_sr_any_cat(self):
        assert is_mixed_relay("SR", "SW") is True

    def test_sprint(self):
        assert is_mixed_relay("SP", "SW") is False


class TestRaceDisplayName:
    def test_womens_sprint(self):
        assert _race_display_name("SP", "SW") == "Women's Sprint"

    def test_mixed_relay(self):
        assert _race_display_name("MR", "MX") == "Mixed Relay"

    def test_single_mixed_relay(self):
        assert _race_display_name("SR", "MX") == "Single Mixed Relay"


class TestSelectRaceInteractive:
    def test_mixed_race_labels_do_not_duplicate_mixed(self, monkeypatch, capsys):
        class _DummyStdin:
            @staticmethod
            def isatty():
                return True

        candidates = [
            (
                "BT2526SWRLCP08MXSR",
                {
                    "Competition": {
                        "catId": "MX",
                        "DisciplineId": "SR",
                        "StartTime": "2026-03-15T11:35:00Z",
                        "StatusText": "Prov. Start List",
                    },
                    "SportEvt": {
                        "Description": "BMW IBU World Cup Biathlon",
                        "Organizer": "Otepaa",
                    },
                },
            ),
            (
                "BT2526SWRLCP08MXRL",
                {
                    "Competition": {
                        "catId": "MX",
                        "DisciplineId": "MR",
                        "StartTime": "2026-03-15T13:40:00Z",
                        "StatusText": "Prov. Start List",
                    },
                    "SportEvt": {
                        "Description": "BMW IBU World Cup Biathlon",
                        "Organizer": "Otepaa",
                    },
                },
            ),
        ]

        monkeypatch.setattr("biathlon.commands._common.sys.stdin", _DummyStdin())
        monkeypatch.setattr(builtins, "input", lambda _prompt: "1")

        race_id, _payload = _select_race_interactive(candidates)

        err = capsys.readouterr().err
        assert race_id == "BT2526SWRLCP08MXSR"
        assert "[World Cup] Single Mixed Relay - Otepaa" in err
        assert "[World Cup] Mixed Relay - Otepaa" in err
        assert "Mixed Single Mixed Relay" not in err
        assert "Mixed Mixed Relay" not in err


class TestCountsTowardWcStandings:
    def test_wc_events_always_count(self):
        assert counts_toward_wc_standings("WC", "2526", "SP", "SW") is True

    def test_olympics_only_count_in_1998_to_2010_window(self):
        assert counts_toward_wc_standings("OWG", "9798", "SP", "SW") is True
        assert counts_toward_wc_standings("OWG", "0102", "SP", "SW") is True
        assert counts_toward_wc_standings("OWG", "0506", "SP", "SW") is True
        assert counts_toward_wc_standings("OWG", "0910", "SP", "SW") is True
        assert counts_toward_wc_standings("OWG", "1314", "SP", "SW") is False
        assert counts_toward_wc_standings("OWG", "9394", "SP", "SW") is False

    def test_wch_basic_yes_no_year_exceptions(self):
        assert counts_toward_wc_standings("WCH", "8990", "SP", "SW") is True
        assert counts_toward_wc_standings("WCH", "9091", "SP", "SW") is False
        assert counts_toward_wc_standings("WCH", "9293", "SP", "SW") is False
        assert counts_toward_wc_standings("WCH", "1516", "SP", "SW") is True
        assert counts_toward_wc_standings("WCH", "1718", "SP", "SW") is False
        assert counts_toward_wc_standings("WCH", "2122", "SP", "SW") is False

    def test_wch_partial_olympic_year_rules_are_discipline_specific(self):
        assert counts_toward_wc_standings("WCH", "9798", "PU", "SW") is True
        assert counts_toward_wc_standings("WCH", "9798", "TM", "SM") is True
        assert counts_toward_wc_standings("WCH", "9798", "SP", "SW") is False

        assert counts_toward_wc_standings("WCH", "0102", "MS", "SM") is True
        assert counts_toward_wc_standings("WCH", "0102", "PU", "SM") is False

        assert counts_toward_wc_standings("WCH", "0506", "MR", "MX") is True
        assert counts_toward_wc_standings("WCH", "0506", "RL", "MX") is True
        assert counts_toward_wc_standings("WCH", "0506", "RL", "SW") is False
