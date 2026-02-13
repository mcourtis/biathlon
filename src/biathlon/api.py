"""Lightweight helper for calling the Biathlon results API."""

from __future__ import annotations

import json
import socket
from collections.abc import Iterable
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import urlopen

API_BASE = "https://biathlonresults.com/modules/sportapi/api"


class BiathlonError(Exception):
    """Raised when the remote API call fails."""


def fetch_json(path: str) -> Iterable[dict[str, Any]]:
    """Retrieve JSON data from the API and decode it."""
    url = f"{API_BASE}/{path}"
    try:
        with urlopen(url, timeout=30) as resp:  # noqa: S310 - trusted domain
            if resp.status != 200:
                raise BiathlonError(f"Request failed: {resp.status} {resp.reason}")
            payload = resp.read()
    except HTTPError as exc:
        raise BiathlonError(f"Request failed: {exc.code} {exc.reason}") from exc
    except URLError as exc:
        raise BiathlonError(f"Network error: {exc.reason}") from exc
    except socket.timeout as exc:
        raise BiathlonError(
            "Request timed out - server may be slow or unreachable"
        ) from exc

    try:
        return json.loads(payload.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise BiathlonError("Could not parse API response as JSON") from exc


def get_seasons() -> list[dict[str, Any]]:
    """Return all seasons from the API."""
    return list(fetch_json("Seasons"))


def get_current_season_id() -> str:
    """Return the current season id (fallback to the newest one)."""
    seasons = get_seasons()
    if not seasons:
        raise BiathlonError("No seasons returned by API")

    current = next((season for season in seasons if season.get("IsCurrent")), None)
    if current:
        return str(current.get("SeasonId"))

    newest = max(seasons, key=lambda season: season.get("SortOrder", 0))
    return str(newest.get("SeasonId"))


def get_events(season_id: str, level: int) -> list[dict[str, Any]]:
    """Return events for a season/level combination."""
    return list(fetch_json(f"Events?SeasonId={season_id}&Level={level}"))


def get_races(event_id: str) -> list[dict[str, Any]]:
    """Return races (competitions) for a given event."""
    return list(fetch_json(f"Competitions?EventId={event_id}"))


def get_race_results(race_id: str) -> dict[str, Any]:
    """Return full results payload for a race."""
    return dict(fetch_json(f"Results?RaceId={race_id}"))


def get_cups(season_id: str) -> list[dict[str, Any]]:
    """Return all cup definitions for a season."""
    return list(fetch_json(f"Cups?SeasonId={season_id}"))


def get_cup_results(cup_id: str) -> dict[str, Any]:
    """Return standings for a cup (score list)."""
    return dict(fetch_json(f"CupResults?CupId={cup_id}"))


def get_analytic_results(race_id: str, type_id: str) -> dict[str, Any]:
    """Return analytic results for a race (e.g., course/range/shooting times)."""
    return dict(fetch_json(f"AnalyticResults?RaceId={race_id}&TypeId={type_id}"))


def get_athlete_bio(ibu_id: str) -> dict[str, Any]:
    """Return CIS bio information for an athlete by IBU id."""
    return dict(fetch_json(f"CISBios?IBUId={ibu_id}"))


def get_athletes(
    family_name: str, given_name: str = "", request_id: int = 0
) -> list[dict[str, Any]]:
    """Search athletes by family/given name."""
    query = urlencode(
        {
            "FamilyName": family_name,
            "GivenName": given_name,
            "RequestId": request_id,
        }
    )
    payload = fetch_json(f"Athletes?{query}")
    if isinstance(payload, dict):
        athletes = payload.get("Athletes") or payload.get("athletes") or []
        return list(athletes)
    return list(payload)


def get_all_results(ibu_id: str) -> dict[str, Any]:
    """Return results list for an athlete by IBU id."""
    return dict(fetch_json(f"AllResults?IBUId={ibu_id}"))
