"""Tests for utility functions."""

import datetime


from biathlon.utils import (
    parse_date,
    parse_time_seconds,
    parse_csv_values,
    parse_misses,
    is_dns,
    get_first_time,
    extract_results,
)


class TestParseDate:
    def test_valid_date(self):
        assert parse_date("2024-01-15") == datetime.date(2024, 1, 15)

    def test_date_with_time(self):
        assert parse_date("2024-01-15T14:30:00") == datetime.date(2024, 1, 15)

    def test_empty_string(self):
        assert parse_date("") is None

    def test_none(self):
        assert parse_date(None) is None

    def test_invalid_date(self):
        assert parse_date("not-a-date") is None


class TestParseTimeSeconds:
    def test_seconds_only(self):
        assert parse_time_seconds("45.5") == 45.5

    def test_minutes_seconds(self):
        assert parse_time_seconds("2:30.5") == 150.5

    def test_hours_minutes_seconds(self):
        assert parse_time_seconds("1:02:30.5") == 3750.5

    def test_with_plus_prefix(self):
        assert parse_time_seconds("+15.3") == 15.3

    def test_empty_string(self):
        assert parse_time_seconds("") is None

    def test_none(self):
        assert parse_time_seconds(None) is None

    def test_invalid_format(self):
        assert parse_time_seconds("invalid") is None


class TestParseCsvValues:
    def test_simple_csv(self):
        assert parse_csv_values("a,b,c") == ["a", "b", "c"]

    def test_with_spaces(self):
        assert parse_csv_values(" a , b , c ") == ["a", "b", "c"]

    def test_empty_parts(self):
        assert parse_csv_values("a,,b") == ["a", "b"]

    def test_single_value(self):
        assert parse_csv_values("single") == ["single"]


class TestParseMisses:
    def test_simple_number(self):
        assert parse_misses("3") == 3

    def test_shooting_format(self):
        assert parse_misses("0+1+2+0") == 3

    def test_with_text(self):
        assert parse_misses("Total: 5") == 5

    def test_none(self):
        assert parse_misses(None) is None

    def test_empty_string(self):
        assert parse_misses("") is None

    def test_no_digits(self):
        assert parse_misses("no digits here") is None


class TestIsDns:
    def test_irm_dns(self):
        assert is_dns({"IRM": "DNS"}) is True

    def test_result_dns(self):
        assert is_dns({"Result": "DNS"}) is True

    def test_total_time_dns(self):
        assert is_dns({"TotalTime": "DNS"}) is True

    def test_lowercase_dns(self):
        assert is_dns({"IRM": "dns"}) is True

    def test_not_dns(self):
        assert is_dns({"IRM": "OK", "Result": "25:30.5"}) is False

    def test_empty_result(self):
        assert is_dns({}) is False


class TestGetFirstTime:
    def test_first_key_exists(self):
        result = {"Time1": "1:30.0", "Time2": "2:00.0"}
        assert get_first_time(result, ["Time1", "Time2"]) == "1:30.0"

    def test_second_key_exists(self):
        result = {"Time2": "2:00.0"}
        assert get_first_time(result, ["Time1", "Time2"]) == "2:00.0"

    def test_no_keys_exist(self):
        result = {"Other": "value"}
        assert get_first_time(result, ["Time1", "Time2"]) == ""

    def test_empty_value_skipped(self):
        result = {"Time1": "", "Time2": "2:00.0"}
        assert get_first_time(result, ["Time1", "Time2"]) == "2:00.0"


class TestExtractResults:
    def test_results_key(self):
        payload = {"Results": [{"Rank": 1, "Name": "A"}, {"Rank": 2, "Name": "B"}]}
        results = extract_results(payload)
        assert len(results) == 2
        assert results[0]["Name"] == "A"

    def test_result_list_key(self):
        payload = {"ResultList": [{"Rank": 1, "Name": "A"}]}
        results = extract_results(payload)
        assert len(results) == 1

    def test_filters_teams(self):
        payload = {
            "Results": [
                {"Rank": 1, "Name": "A"},
                {"Rank": 2, "Name": "Team", "IsTeam": True},
            ]
        }
        results = extract_results(payload)
        assert len(results) == 1
        assert results[0]["Name"] == "A"

    def test_empty_payload(self):
        assert extract_results({}) == []

    def test_sorts_by_rank(self):
        payload = {"Results": [{"Rank": 2, "Name": "B"}, {"Rank": 1, "Name": "A"}]}
        results = extract_results(payload)
        assert results[0]["Name"] == "A"
        assert results[1]["Name"] == "B"
