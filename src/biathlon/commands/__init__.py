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
    handle_cumulate_cleansheet,
    handle_cumulate_remontada,
)
from .standings import handle_standings
from .ceremony import handle_ceremony
from .achievements import handle_achievements
from .athlete import (
    handle_athlete_results,
    handle_athlete_results_scan,
    handle_athlete_info,
    handle_athlete_id,
)
from .shooting import handle_shooting
from .postrace import handle_post_race
from .brief import (
    handle_brief_preevent,
    handle_brief_postevent,
    handle_brief_preseason,
    handle_brief_postseason,
    handle_brief_startlist,
    handle_brief_postrace,
)
from .form import handle_form

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
    "handle_cumulate_cleansheet",
    "handle_cumulate_remontada",
    "handle_standings",
    "handle_ceremony",
    "handle_achievements",
    "handle_athlete_results",
    "handle_athlete_results_scan",
    "handle_athlete_info",
    "handle_athlete_id",
    "handle_shooting",
    "handle_post_race",
    "handle_brief_preevent",
    "handle_brief_postevent",
    "handle_brief_preseason",
    "handle_brief_postseason",
    "handle_brief_startlist",
    "handle_brief_postrace",
    "handle_form",
]
