"""CLI entry point for Biathlon results."""

from __future__ import annotations

import argparse
import textwrap
import sys
from collections.abc import Iterable
from importlib.metadata import version, PackageNotFoundError

from .api import BiathlonError
from .commands import (
    handle_athlete_id,
    handle_athlete_info,
    handle_athlete_results,
    handle_brief_event,
    handle_brief_post_race,
    handle_brief_season,
    handle_brief_startlist,
    handle_ceremony,
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
    handle_events,
    handle_form,
    handle_results,
    handle_standings,
    handle_seasons,
    handle_shooting,
)


def get_version() -> str:
    """Get package version."""
    try:
        return version("biathlon")
    except PackageNotFoundError:
        return "dev"


class CompactOptionalFormatter(argparse.RawTextHelpFormatter):
    """Formatter that groups optional flags before their metavar."""

    def __init__(
        self,
        prog: str,
        indent_increment: int = 2,
        max_help_position: int = 40,
        width: int | None = None,
    ) -> None:
        super().__init__(prog, indent_increment, max_help_position, width)

    def _format_action_invocation(self, action: argparse.Action) -> str:
        if not action.option_strings:
            return super()._format_action_invocation(action)
        opts = ", ".join(action.option_strings)
        if action.nargs != 0:
            opts += f" {self._format_args(action, action.dest.upper())}"
        return opts

    def _format_action(self, action: argparse.Action) -> str:
        if isinstance(action, argparse._SubParsersAction):
            parts = []
            subactions = list(action._get_subactions())
            self._indent()
            for subaction in subactions:
                if not subaction.help:
                    continue
                parts.append(super()._format_action(subaction))
            self._dedent()
            return "".join(parts)
        return super()._format_action(action)


def traverse_to_parser(
    parser: argparse.ArgumentParser,
    tokens: list[str],
) -> tuple[argparse.ArgumentParser, list[str]]:
    """Traverse subparsers to the deepest matching parser."""
    if not parser._subparsers:
        return parser, tokens
    subparsers_action = None
    for action in parser._subparsers._actions:
        if isinstance(action, argparse._SubParsersAction):
            subparsers_action = action
            break
    if not subparsers_action or not tokens:
        return parser, tokens
    command = tokens[0]
    choices = subparsers_action.choices
    if command not in choices:
        return parser, tokens
    return traverse_to_parser(choices[command], tokens[1:])


def add_output_format_arg(subparser: argparse.ArgumentParser) -> None:
    """Add --format flag to a subparser."""
    subparser.add_argument(
        "--format",
        choices=["tsv", "markdown"],
        default="",
        metavar="FORMAT",
        help="Output format (tsv, markdown). Default: aligned table",
    )


def add_cumulate_args(
    subparser: argparse.ArgumentParser, allow_discipline_event: bool
) -> None:
    """Add common cumulate arguments to a subparser."""
    subparser.add_argument(
        "--men",
        action="store_true",
        help="Show men (default: women)",
    )
    if allow_discipline_event:
        subparser.add_argument(
            "--discipline",
            default="all",
            choices=[
                "individual",
                "sprint",
                "pursuit",
                "mass-start",
                "relay",
                "mixed-relay",
                "single-mixed-relay",
                "all",
            ],
            metavar="DISCIPLINE",
            help="Race discipline (default: all)",
        )
        event_help = "Event id (overrides --discipline and --season)"
    else:
        event_help = "Event id (only supported for cumulate results)"
    subparser.add_argument(
        "--event",
        default="",
        help=event_help,
    )
    subparser.add_argument(
        "--season",
        default="",
        help="Season id (default: current season)",
    )
    subparser.add_argument(
        "--include-relay",
        action="store_true",
        help="Include relay races in cumulate calculations",
    )
    subparser.add_argument(
        "--top",
        type=int,
        default=0,
        help="Filter to top N athletes in WC standings",
    )
    subparser.add_argument(
        "--limit",
        type=int,
        default=25,
        help="Number of rows to display (default: 25, 0 for all)",
    )
    add_output_format_arg(subparser)


def build_parser() -> argparse.ArgumentParser:
    """Build the main argument parser with all subcommands."""
    parser = argparse.ArgumentParser(
        prog="biathlon",
        description="CLI for exploring IBU biathlon results stored on biathlonresults.com",
        usage="\n    biathlon command [subcommand] [parameters]",
        add_help=False,
        formatter_class=CompactOptionalFormatter,
        epilog=textwrap.dedent("""\
            Examples:
                biathlon events --races           Get events and races for the current season
                biathlon results --men --detail   Get detailed results for the most recent men race
                biathlon cumulate remontada       Get women biathlete with biggest pursuit gains this season
                biathlon standings help           Get help for standings command
        """),
    )
    parser._positionals.title = "Available commands"
    subparsers = parser.add_subparsers(dest="command")

    # --- seasons ---
    seasons_parser = subparsers.add_parser(
        "seasons",
        help="List seasons available",
        usage="\n  biathlon seasons [parameters]",
        formatter_class=CompactOptionalFormatter,
        add_help=False,
    )
    seasons_parser._optionals.title = "optional parameters"
    seasons_parser.add_argument(
        "--limit",
        metavar="INT",
        type=int,
        default=25,
        help="Limit output rows (default: 25, 0 for all)",
    )
    add_output_format_arg(seasons_parser)
    seasons_parser.set_defaults(func=handle_seasons)

    # --- events ---
    events_parser = subparsers.add_parser(
        "events",
        help="List season events",
        formatter_class=CompactOptionalFormatter,
        add_help=False,
    )
    events_parser._optionals.title = "optional parameters"
    events_parser.add_argument(
        "--season",
        default="",
        help="Select specific season Id or 'all' (default: current season)",
    )
    events_parser.add_argument(
        "--level",
        default="1",
        help="Select specific competitien levels: -1=All, 0=Mixed, 1=World Cup (default), 2=IBU Cup, 3=Junior, 4=Other, 5=Regional, 6=Para",
    )
    events_parser.add_argument("--search", default="", help="Filter events by name")
    events_parser.add_argument("--sort", default="startdate", help="Sort order")
    events_parser.add_argument(
        "--completed", action="store_true", help="Only completed events"
    )
    events_parser.add_argument(
        "--upcoming", action="store_true", help="Only current/next and upcoming events"
    )
    events_parser.add_argument(
        "--summary", action="store_true", help="Show race-type availability per event"
    )
    events_parser.add_argument(
        "--races", action="store_true", help="Include races under each event"
    )
    events_parser.add_argument(
        "--discipline",
        default="",
        help="Filter races by discipline (individual, sprint, pursuit, mass-start, relay, mixed-relay, single-mixed-relay)",
    )
    add_output_format_arg(events_parser)
    events_parser.set_defaults(func=handle_events)

    # --- results ---
    results_parser = subparsers.add_parser(
        "results",
        help="Show race results",
        formatter_class=CompactOptionalFormatter,
        add_help=False,
    )
    results_parser.add_argument(
        "--race", default="", help="Race id (default: most recent race)"
    )
    results_parser.add_argument(
        "--men", action="store_true", help="Show men (default: women)"
    )
    results_parser.add_argument(
        "--discipline",
        default="",
        help="Discipline filter (mutually exclusive with --race)",
    )
    results_parser.add_argument(
        "--detail", action="store_true", help="Show detailed split columns"
    )
    results_parser.add_argument("--sort", default="", help="Sort by column")
    results_parser.add_argument(
        "--country",
        default="",
        metavar="COUNTRY",
        help="Filter by country code (e.g., FRA, GER, NOR)",
    )
    results_parser.add_argument(
        "--top",
        type=int,
        default=0,
        help="Filter to top N athletes in World Cup standings",
    )
    results_parser.add_argument(
        "--first", type=int, default=0, help="Filter to first N finishers in the race"
    )
    results_parser.add_argument(
        "--limit",
        type=int,
        default=25,
        help="Limit output rows (default: 25, 0 for all)",
    )
    results_parser.add_argument(
        "--highlight-wc",
        action="store_true",
        help="Highlight top 6 by World Cup standing instead of race rank",
    )
    add_output_format_arg(results_parser)
    results_parser.set_defaults(func=handle_results)

    # --- cumulate ---
    cumulate_parser = subparsers.add_parser(
        "cumulate",
        help="Show cumulative rankings (results, course, miss, position gain, etc.)",
        formatter_class=CompactOptionalFormatter,
        add_help=False,
    )

    cumulate_help: dict[str, str] = {}

    def _show_cumulate_help(_args: argparse.Namespace) -> int:
        print("usage: biathlon cumulate <subcommand> [options]\n")
        print("subcommands:")
        width = max((len(name) for name in cumulate_sub.choices.keys()), default=0)
        for name in cumulate_sub.choices.keys():
            help_text = cumulate_help.get(name, "")
            print(f"  {name.ljust(width)}  {help_text}")
        return 0

    add_cumulate_args(cumulate_parser, allow_discipline_event=True)
    cumulate_parser._custom_help = _show_cumulate_help  # type: ignore[attr-defined]
    cumulate_parser.set_defaults(func=_show_cumulate_help, cumulate_command=None)
    cumulate_sub = cumulate_parser.add_subparsers(
        dest="cumulate_command", title="subcommands", metavar=""
    )

    cumulate_results = cumulate_sub.add_parser(
        "results",
        help="Cumulated results",
        formatter_class=CompactOptionalFormatter,
        add_help=False,
    )
    cumulate_help["results"] = "Cumulated race results"
    add_cumulate_args(cumulate_results, allow_discipline_event=True)
    cumulate_results.set_defaults(func=handle_cumulate_results)

    cumulate_ski = cumulate_sub.add_parser(
        "ski",
        help="Cumulated ski times",
        formatter_class=CompactOptionalFormatter,
        add_help=False,
    )
    cumulate_help["ski"] = (
        "Cumulated ski times (individual races only ; results without penalties)"
    )
    add_cumulate_args(cumulate_ski, allow_discipline_event=False)
    cumulate_ski.set_defaults(func=handle_cumulate_ski)

    cumulate_pursuit = cumulate_sub.add_parser(
        "pursuit",
        help="Cumulated pursuit times",
        formatter_class=CompactOptionalFormatter,
        add_help=False,
    )
    cumulate_help["pursuit"] = (
        "Cumulated pursuit times (pursuit races only ; results without start delay)"
    )
    add_cumulate_args(cumulate_pursuit, allow_discipline_event=False)
    cumulate_pursuit.set_defaults(func=handle_cumulate_pursuit)

    cumulate_course = cumulate_sub.add_parser(
        "course",
        help="Cumulated course times",
        formatter_class=CompactOptionalFormatter,
        add_help=False,
    )
    cumulate_help["course"] = (
        "Cumulated course times (ski time only ; range, start delay, penalties excluded)"
    )
    add_cumulate_args(cumulate_course, allow_discipline_event=True)
    cumulate_course.set_defaults(func=handle_cumulate_course)

    cumulate_range = cumulate_sub.add_parser(
        "range",
        help="Cumulated range times",
        formatter_class=CompactOptionalFormatter,
        add_help=False,
    )
    cumulate_help["range"] = "Cumulated range times"
    add_cumulate_args(cumulate_range, allow_discipline_event=True)
    cumulate_range.set_defaults(func=handle_cumulate_range)

    cumulate_shooting = cumulate_sub.add_parser(
        "shooting",
        help="Cumulated shooting times",
        formatter_class=CompactOptionalFormatter,
        add_help=False,
    )
    cumulate_help["shooting"] = "Cumulated shooting times"
    add_cumulate_args(cumulate_shooting, allow_discipline_event=True)
    cumulate_shooting.set_defaults(func=handle_cumulate_shooting)

    cumulate_miss = cumulate_sub.add_parser(
        "miss",
        help="Cumulated misses",
        formatter_class=CompactOptionalFormatter,
        add_help=False,
    )
    cumulate_help["miss"] = "Cumulated misses"
    add_cumulate_args(cumulate_miss, allow_discipline_event=True)
    cumulate_miss.set_defaults(func=handle_cumulate_miss)

    cumulate_penalty = cumulate_sub.add_parser(
        "penalty",
        help="Cumulated penalty times",
        formatter_class=CompactOptionalFormatter,
        add_help=False,
    )
    cumulate_help["penalty"] = "Cumulated penalty times"
    add_cumulate_args(cumulate_penalty, allow_discipline_event=True)
    cumulate_penalty.set_defaults(func=handle_cumulate_penalty)

    cumulate_remontada = cumulate_sub.add_parser(
        "remontada",
        help="Cumulated pursuit gains",
        formatter_class=CompactOptionalFormatter,
        add_help=False,
    )
    cumulate_help["remontada"] = "Cumulated pursuit gains"
    add_cumulate_args(cumulate_remontada, allow_discipline_event=False)
    cumulate_remontada.set_defaults(func=handle_cumulate_remontada)

    cumulate_cleansheet = cumulate_sub.add_parser(
        "cleansheet",
        help="Cumulated clean shooting stages (5/5)",
        formatter_class=CompactOptionalFormatter,
        add_help=False,
    )
    cumulate_help["cleansheet"] = "Cumulated clean shooting stages (5/5)"
    add_cumulate_args(cumulate_cleansheet, allow_discipline_event=True)
    cumulate_cleansheet.add_argument(
        "--sort",
        default="cleansheets",
        choices=["cleansheets", "percentage", "time"],
        help="Sort by column (default: cleansheets)",
    )
    cumulate_cleansheet.add_argument(
        "--min",
        type=int,
        default=66,
        dest="min_pct",
        metavar="PCT",
        help="Minimum race participation percentage (0-100, default: 50)",
    )
    cumulate_cleansheet.set_defaults(func=handle_cumulate_cleansheet)

    # --- standings ---
    standings_parser = subparsers.add_parser(
        "standings",
        help="Show standings (world cup, IBU Cup, etc.)",
        formatter_class=CompactOptionalFormatter,
        add_help=False,
    )
    standings_parser.add_argument("--season", default="", help="Season id")
    standings_parser.add_argument("--men", action="store_true", help="Show men")
    standings_parser.add_argument("--level", default="1", help="Cup level")
    standings_parser.add_argument(
        "--sort",
        default="total",
        choices=["total", "sprint", "pursuit", "individual", "massstart"],
        help="Sort by column",
    )
    standings_parser.add_argument(
        "--limit",
        type=int,
        default=25,
        help="Limit output rows (default: 25, 0 for all)",
    )
    add_output_format_arg(standings_parser)
    standings_parser.set_defaults(func=handle_standings)

    # --- ceremony ---
    ceremony_parser = subparsers.add_parser(
        "ceremony",
        help="Show medal standing (default: men+women, World Cup, current season)",
        formatter_class=CompactOptionalFormatter,
        add_help=False,
    )
    ceremony_parser.add_argument(
        "--athlete", action="store_true", help="Rank by athlete (default: by country)"
    )
    ceremony_parser.add_argument("--race", default="", help="Race id")
    ceremony_parser.add_argument("--event", default="", help="Event id")
    gender_group = ceremony_parser.add_mutually_exclusive_group()
    gender_group.add_argument(
        "--men", action="store_true", help="Show men only (default: men+women)"
    )
    gender_group.add_argument(
        "--women", action="store_true", help="Show women only (default: men+women)"
    )
    ceremony_parser.add_argument(
        "--country", default="", help="Filter by host country (where event is held)"
    )
    ceremony_parser.add_argument(
        "--search",
        default="",
        help="Filter events by name (e.g., 'annecy', 'holmenkollen')",
    )
    ceremony_parser.add_argument(
        "--season", default="", help="Season id (default: current season)"
    )
    add_output_format_arg(ceremony_parser)
    ceremony_parser.set_defaults(func=handle_ceremony)

    # --- shooting ---
    shooting_parser = subparsers.add_parser(
        "shooting",
        help="Show shooting accuracy",
        formatter_class=CompactOptionalFormatter,
        add_help=False,
    )
    shooting_parser.add_argument("--race", default="", help="Race id")
    shooting_parser.add_argument("--event", default="", help="Event id")
    shooting_parser.add_argument("--season", default="", help="Season id")
    shooting_parser.add_argument("--men", action="store_true", help="Show men")
    shooting_parser.add_argument(
        "--include-relay",
        default="",
        choices=["relay", "mixed-relay", "single-mixed", "all", ""],
        help="Include relay races in shooting stats (relay, mixed-relay, single-mixed, all)",
    )
    shooting_parser.add_argument(
        "--all-races", action="store_true", help="Only athletes who started every race"
    )
    shooting_parser.add_argument("--sort", default="", help="Sort order")
    shooting_parser.add_argument(
        "--min",
        type=int,
        default=50,
        dest="min_pct",
        help="Minimum race participation %% (default: 50)",
    )
    shooting_parser.add_argument(
        "--top", type=int, default=0, help="Restrict to top N athletes in WC standings"
    )
    shooting_parser.add_argument(
        "--limit",
        type=int,
        default=25,
        help="Limit output rows (default: 25, 0 for all)",
    )
    shooting_parser.add_argument(
        "--debug-races", action="store_true", help="Debug: print races considered"
    )
    add_output_format_arg(shooting_parser)
    shooting_parser.set_defaults(func=handle_shooting)

    # --- athlete ---
    athlete_parser = subparsers.add_parser(
        "athlete",
        help="Show athlete information",
        formatter_class=CompactOptionalFormatter,
        add_help=False,
    )
    athlete_parser.add_argument(
        "--id", default="", help="Athlete IBU id (comma-separated)"
    )
    athlete_parser.add_argument("--search", default="", help="Search by name")
    athlete_parser.add_argument("--season", default="", help="Season id")
    add_output_format_arg(athlete_parser)
    athlete_parser.set_defaults(func=handle_athlete_info, athlete_command=None)
    athlete_sub = athlete_parser.add_subparsers(
        dest="athlete_command", title="subcommands", metavar=""
    )

    athlete_results = athlete_sub.add_parser(
        "results",
        help="Season race ranks (AllResults)",
        formatter_class=CompactOptionalFormatter,
        add_help=False,
    )
    athlete_results.add_argument("--id", default="", help="Athlete IBU id")
    athlete_results.add_argument(
        "--season", default="", help="Season id or label (e.g., 2526 or 25/26)"
    )
    athlete_results.add_argument(
        "--level",
        default="WC,OWG,WCH",
        help="Level filter (default: WC,OWG,WCH — use 'all' for all levels)",
    )
    athlete_results.add_argument(
        "--course", action="store_true", help="Use course time rank"
    )
    add_output_format_arg(athlete_results)
    athlete_results.set_defaults(func=handle_athlete_results)

    athlete_info = athlete_sub.add_parser(
        "info",
        help="Athlete bio info",
        formatter_class=CompactOptionalFormatter,
        add_help=False,
    )
    athlete_info.add_argument(
        "--id", default="", help="Athlete IBU id (comma-separated)"
    )
    athlete_info.add_argument("--search", default="", help="Search by name")
    athlete_info.add_argument("--season", default="", help="Season id")
    athlete_info.add_argument(
        "--level", type=int, default=0, help="Event level (1-5, 0 for all)"
    )
    add_output_format_arg(athlete_info)
    athlete_info.set_defaults(func=handle_athlete_info)

    athlete_id = athlete_sub.add_parser(
        "id",
        help="Find athlete IBU ids",
        formatter_class=CompactOptionalFormatter,
        add_help=False,
    )
    athlete_id.add_argument("--search", default="", help="Search by name")
    athlete_id.add_argument("--season", default="", help="Season id")
    athlete_id.add_argument(
        "--level", type=int, default=0, help="Event level (1-5, 0 for all)"
    )
    add_output_format_arg(athlete_id)
    athlete_id.set_defaults(func=handle_athlete_id)

    # --- brief ---
    brief_parser = subparsers.add_parser(
        "brief",
        help="Race analysis (event, season, startlist, postrace)",
        formatter_class=CompactOptionalFormatter,
        add_help=False,
    )
    brief_help: dict[str, str] = {}

    def _show_brief_help(_args: argparse.Namespace) -> int:
        print("usage: biathlon brief <subcommand> [options]\n")
        print("subcommands:")
        width = max((len(name) for name in brief_sub.choices.keys()), default=0)
        for name in brief_sub.choices.keys():
            help_text = brief_help.get(name, "")
            print(f"  {name.ljust(width)}  {help_text}")
        return 0

    brief_parser._custom_help = _show_brief_help  # type: ignore[attr-defined]
    brief_parser.set_defaults(func=_show_brief_help, brief_command=None)
    brief_sub = brief_parser.add_subparsers(
        dest="brief_command", title="subcommands", metavar=""
    )

    # brief event
    brief_event = brief_sub.add_parser(
        "event",
        help="Venue history and records (before an event)",
        formatter_class=CompactOptionalFormatter,
        add_help=False,
    )
    brief_help["event"] = "Venue history and records (before an event)"
    brief_event.add_argument(
        "--event", default="", help="Event id (default: current/upcoming WC event)"
    )
    brief_event.add_argument(
        "--men", action="store_true", help="Show men (default: women)"
    )
    brief_event.add_argument(
        "--major", action="store_true", help="Use WC+WCH+OWG stats"
    )
    add_output_format_arg(brief_event)
    brief_event.set_defaults(func=handle_brief_event)

    # brief season
    brief_season = brief_sub.add_parser(
        "season",
        help="Season summary",
        formatter_class=CompactOptionalFormatter,
        add_help=False,
    )
    brief_help["season"] = "Season summary (events and race counts)"
    brief_season.add_argument(
        "--season", default="", help="Season id (default: current season)"
    )
    brief_season.add_argument("--level", default="1", help="Event level (default: 1)")
    add_output_format_arg(brief_season)
    brief_season.set_defaults(func=handle_brief_season)

    # brief startlist
    brief_startlist = brief_sub.add_parser(
        "startlist",
        help="Startlist analysis (before a race)",
        formatter_class=CompactOptionalFormatter,
        add_help=False,
    )
    brief_help["startlist"] = "Startlist analysis (before a race)"
    brief_startlist.add_argument(
        "--race", default="", help="Race id (default: latest WC startlist)"
    )
    brief_startlist.add_argument(
        "--major", action="store_true", help="Use WC+WCH+OWG milestones"
    )
    add_output_format_arg(brief_startlist)
    brief_startlist.set_defaults(func=handle_brief_startlist)

    # brief postrace
    brief_post_race = brief_sub.add_parser(
        "postrace",
        help="Post-race analysis (after a race)",
        formatter_class=CompactOptionalFormatter,
        add_help=False,
    )
    brief_help["postrace"] = "Post-race analysis (after a race)"
    brief_post_race.add_argument(
        "--race", default="", help="Race id (default: latest completed race)"
    )
    brief_post_race.add_argument(
        "--major", action="store_true", help="Use WC+WCH+OWG milestones"
    )
    add_output_format_arg(brief_post_race)
    brief_post_race.set_defaults(func=handle_brief_post_race)

    # --- form ---
    form_parser = subparsers.add_parser(
        "form",
        help="Show recent athlete form (course time ranks)",
        formatter_class=CompactOptionalFormatter,
        add_help=False,
    )
    form_parser._optionals.title = "optional parameters"
    form_parser.add_argument(
        "--men", action="store_true", help="Show men (default: women)"
    )
    form_parser.add_argument(
        "--startlist",
        nargs="?",
        const="",
        default=None,
        help="Filter to athletes from a race startlist (auto-detects gender). Without a race id, discovers available startlists.",
    )
    form_parser.add_argument(
        "--races",
        type=int,
        default=5,
        help="Number of recent races for current form (default: 5)",
    )
    form_parser.add_argument(
        "--event",
        type=int,
        default=0,
        metavar="N",
        help="Current form from last N events (mutually exclusive with --races)",
    )
    form_parser.add_argument(
        "--limit",
        type=int,
        default=25,
        help="Limit output rows (default: 25, 0 for all)",
    )
    form_parser.add_argument(
        "--top", type=int, default=0, help="Filter to top N in WC standings"
    )
    form_parser.add_argument(
        "--min",
        type=int,
        default=50,
        dest="min_pct",
        metavar="PCT",
        help="Minimum race participation percentage (0-100, default: 50)",
    )
    form_parser.add_argument(
        "--season", action="store_true", help="Calculate form based on all season races"
    )
    form_parser.add_argument(
        "--remove",
        action="append",
        default=[],
        metavar="DISC",
        help="Remove discipline from calculations (sprint, pursuit, individual, mass-start). Can be repeated.",
    )
    form_parser.add_argument(
        "--include-relay",
        default="",
        choices=["relay", "mixed-relay", "single-mixed", "all", ""],
        help="Include relay races (relay, mixed-relay, single-mixed, all)",
    )
    add_output_format_arg(form_parser)
    form_parser.set_defaults(func=handle_form)

    return parser


def main(argv: Iterable[str] | None = None) -> int:
    """Main CLI entry point."""
    tokens = list(argv) if argv is not None else sys.argv[1:]

    # Handle --version before parsing
    if tokens and tokens[0] in ("--version", "-V"):
        print(f"biathlon {get_version()}")
        return 0

    parser = build_parser()

    if tokens and tokens[-1] == "help":
        target_tokens = tokens[:-1]
        target_parser, remaining = traverse_to_parser(parser, target_tokens)
        if remaining:
            print(f"error: unknown command {' '.join(remaining)}", file=sys.stderr)
            return 1
        custom_help = getattr(target_parser, "_custom_help", None)
        if custom_help:
            print()
            return custom_help(argparse.Namespace())
        print()
        target_parser.print_help()
        print()
        return 0

    args = parser.parse_args(tokens)

    if args.command is None:
        print(
            "\nbiathlon: [ERROR]: the following arguments are required: <command>\n\n"
            "Usage: biathlon <command> [<subcommand>] [parameters]\n\n"
            "Example: biathlon events --races\n\n"
            "To see help text, you can run:\n"
            "  biathlon help\n"
            "  biathlon <command> help\n"
            "  biathlon <command> <subcommand> help\n",
            file=sys.stderr,
        )
        return 2

    if _require_subcommand(args):
        return 2

    try:
        return args.func(args)
    except KeyboardInterrupt:
        print()  # Print newline to clean up partial output
        return 130
    except BrokenPipeError:
        return 0
    except BiathlonError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _require_subcommand(args: argparse.Namespace) -> bool:
    """Return True if a required subcommand is missing."""
    if args.command == "cumulate" and getattr(args, "cumulate_command", None) is None:
        print(
            "\nbiathlon cumulate: [ERROR]: the following arguments are required: <subcommand>\n\n"
            "Usage: biathlon cumulate <subcommand> [parameters]\n\n"
            "Example: biathlon cumulate results --detail\n\n"
            "To see help text, you can run:\n"
            "  biathlon cumulate help\n"
            "  biathlon cumulate <subcommand> help\n",
            file=sys.stderr,
        )
        return True
    if args.command == "athlete" and getattr(args, "athlete_command", None) is None:
        print(
            "\nbiathlon athlete: [ERROR]: the following arguments are required: <subcommand>\n\n"
            "Usage: biathlon athlete <subcommand> [parameters]\n\n"
            'Example: biathlon athlete info --search "Boe"\n\n'
            "To see help text, you can run:\n"
            "  biathlon athlete help\n"
            "  biathlon athlete <subcommand> help\n",
            file=sys.stderr,
        )
        return True
    if args.command == "brief" and getattr(args, "brief_command", None) is None:
        print(
            "\nbiathlon brief: [ERROR]: the following arguments are required: <subcommand>\n\n"
            "Usage: biathlon brief <subcommand> [parameters]\n\n"
            "Example: biathlon brief startlist\n\n"
            "To see help text, you can run:\n"
            "  biathlon brief help\n"
            "  biathlon brief <subcommand> help\n",
            file=sys.stderr,
        )
        return True
    return False


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
