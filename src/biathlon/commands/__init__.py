"""Command handlers for the Biathlon CLI."""

from .seasons import handle_seasons
from .events import handle_events
from .results import handle_results
from .cumulate import (
    handle_cumulate_results,
    handle_cumulate_ski,
    handle_cumulate_pursuit,
    handle_cumulate_course,
    handle_cumulate_range,
    handle_cumulate_shooting,
    handle_cumulate_miss,
    handle_cumulate_penalty,
    handle_cumulate_remontada,
)
from .scores import handle_scores
from .records import handle_record_lap
from .ceremony import handle_ceremony
from .athlete import handle_athlete_results, handle_athlete_results_scan, handle_athlete_info, handle_athlete_id
from .shooting import handle_shooting
from .relay import handle_relay
from .startlist import handle_startlist
from .post_race import handle_post_race

__all__ = [
    "handle_seasons",
    "handle_events",
    "handle_results",
    "handle_cumulate_results",
    "handle_cumulate_ski",
    "handle_cumulate_pursuit",
    "handle_cumulate_course",
    "handle_cumulate_range",
    "handle_cumulate_shooting",
    "handle_cumulate_miss",
    "handle_cumulate_penalty",
    "handle_cumulate_remontada",
    "handle_scores",
    "handle_record_lap",
    "handle_ceremony",
    "handle_athlete_results",
    "handle_athlete_results_scan",
    "handle_athlete_info",
    "handle_athlete_id",
    "handle_shooting",
    "handle_relay",
    "handle_startlist",
    "handle_post_race",
]
