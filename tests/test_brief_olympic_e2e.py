"""End-to-end brief command regression for Olympic medal cutoff behavior."""

from __future__ import annotations

from biathlon.api import BiathlonError
from biathlon.cli import build_parser
from biathlon.commands import brief, post_race, startlist


def _race_payload(
    start_time: str,
    winner_name: str,
    winner_nat: str,
    winner_id: str,
    second_name: str,
    second_nat: str,
    second_id: str,
    third_name: str,
    third_nat: str,
    third_id: str,
) -> dict:
    return {
        "IsStartList": False,
        "IsResult": True,
        "Competition": {
            "DisciplineId": "PU",
            "catId": "SW",
            "StartTime": start_time,
        },
        "SportEvt": {
            "SeasonId": "2526",
            "Description": "Olympic Winter Games",
            "Organizer": "Antholz",
        },
        "Results": [
            {
                "IsTeam": False,
                "Rank": "1",
                "Bib": "1",
                "IBUId": winner_id,
                "Name": winner_name,
                "Nat": winner_nat,
            },
            {
                "IsTeam": False,
                "Rank": "2",
                "Bib": "2",
                "IBUId": second_id,
                "Name": second_name,
                "Nat": second_nat,
            },
            {
                "IsTeam": False,
                "Rank": "3",
                "Bib": "3",
                "IBUId": third_id,
                "Name": third_name,
                "Nat": third_nat,
            },
        ],
    }


def test_brief_startlist_vs_postrace_olympic_medal_cutoff(monkeypatch, capsys):
    target_race_id = "BT2526SWRLOG__SWPU"
    previous_race_id = "BT2526SWRLOG__SWPU_PREV"

    previous_payload = _race_payload(
        start_time="2026-02-14T12:00:00Z",
        winner_name="Ingrid Tandrevold",
        winner_nat="NOR",
        winner_id="NOR1",
        second_name="Hanna Oeberg",
        second_nat="SWE",
        second_id="SWE2",
        third_name="Franziska Preuss",
        third_nat="GER",
        third_id="GER1",
    )
    target_payload = _race_payload(
        start_time="2026-02-15T12:00:00Z",
        winner_name="Lisa Vittozzi",
        winner_nat="ITA",
        winner_id="ITA1",
        second_name="Lou Jeanmonnot",
        second_nat="FRA",
        second_id="FRA1",
        third_name="Elvira Oeberg",
        third_nat="SWE",
        third_id="SWE1",
    )

    race_payloads = {
        target_race_id: target_payload,
        previous_race_id: previous_payload,
    }

    def fake_get_race_results(race_id: str) -> dict:
        payload = race_payloads.get(race_id)
        if payload is None:
            raise BiathlonError(f"unknown race {race_id}")
        return payload

    def fake_get_races(event_id: str) -> list[dict]:
        if event_id != "BT2526SWRLOG__":
            raise BiathlonError(f"unknown event {event_id}")
        return [
            {
                "RaceId": previous_race_id,
                "DisciplineId": "PU",
                "catId": "SW",
                "StartTime": "2026-02-14T12:00:00Z",
            },
            {
                "RaceId": target_race_id,
                "DisciplineId": "PU",
                "catId": "SW",
                "StartTime": "2026-02-15T12:00:00Z",
            },
        ]

    monkeypatch.setattr(brief, "get_race_results", fake_get_race_results)
    monkeypatch.setattr(post_race, "get_race_results", fake_get_race_results)
    monkeypatch.setattr(startlist, "get_race_results", fake_get_race_results)
    monkeypatch.setattr(startlist, "get_races", fake_get_races)
    monkeypatch.setattr(startlist, "OLYMPIC_SEASON_IDS", ["2526"])

    # Keep the test focused on Olympic medal sections.
    monkeypatch.setattr(startlist, "_fetch_age", lambda ibu_id: (ibu_id, "-"))
    monkeypatch.setattr(
        startlist,
        "_fetch_athlete_results",
        lambda ibu_id: (ibu_id, {"Results": []}),
    )
    monkeypatch.setattr(
        startlist, "_compute_wc_pre_race_standings", lambda *a, **k: ([], [])
    )
    monkeypatch.setattr(
        startlist, "_render_wc_standings_sections", lambda *a, **k: None
    )
    monkeypatch.setattr(
        post_race,
        "_render_best_performances_section",
        lambda _args, sec, *_a, **_k: sec,
    )
    monkeypatch.setattr(post_race, "_collect_discipline_race_ids", lambda *a, **k: [])

    parser = build_parser()

    startlist_args = parser.parse_args(
        ["brief", "startlist", "--race", target_race_id, "--format", "tsv"]
    )
    assert startlist_args.func(startlist_args) == 0
    startlist_out = capsys.readouterr().out

    postrace_args = parser.parse_args(
        ["brief", "postrace", "--race", target_race_id, "--format", "tsv"]
    )
    assert postrace_args.func(postrace_args) == 0
    postrace_out = capsys.readouterr().out

    # Pre-race startlist output must exclude current-race medals.
    assert "Country medal table (Women, all Olympic disciplines):" in startlist_out
    assert "Italy\t1\t0\t0\t1" not in startlist_out
    assert "Lisa Vittozzi\tITA\tF\t1\t0\t0\t1" not in startlist_out

    # Post-race output must include current-race medals.
    assert "Country medal table (all Olympic disciplines):" in postrace_out
    assert "Italy\t1\t0\t0\t1" in postrace_out
    assert "Lisa Vittozzi\tITA\tF\t1\t0\t0\t1" in postrace_out
