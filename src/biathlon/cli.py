"""CLI entry point for Biathlon results."""

from __future__ import annotations

import argparse
import textwrap
import sys
from collections.abc import Iterable
from importlib.metadata import version, PackageNotFoundError
import io
import contextlib

from .api import BiathlonError
from .markdown import to_markdown_table
<<<<<<< HEAD
from .commands import (
    handle_achievements,
    handle_athlete_id,
    handle_athlete_info,
    handle_athlete_results,
    handle_brief_preevent,
    handle_brief_postevent,
    handle_brief_preseason,
    handle_brief_postseason,
    handle_brief_postrace,
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
=======
>>>>>>> bd1e6ea (modification in cli to support markdown output)


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
            arg_text = self._format_args(action, action.dest.upper())
            if arg_text:
                opts += f" {arg_text}"
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


<<<<<<< HEAD
def add_output_format_arg(subparser: argparse.ArgumentParser) -> None:
=======
BASH_COMPLETION = """
_biathlon_completion() {
    local cur prev commands subcommands
    COMPREPLY=()
    cur="${COMP_WORDS[COMP_CWORD]}"
    prev="${COMP_WORDS[COMP_CWORD-1]}"

    commands="seasons events results cumulate record standings ceremony biathlete shooting relay"

    case "${COMP_WORDS[1]}" in
        results)
            subcommands="remontada ski range shooting"
            ;;
        cumulate)
            subcommands="course ski range shooting miss remontada"
            ;;
        record)
            subcommands="lap"
            ;;
        biathlete)
            subcommands="info results"
            ;;
        *)
            subcommands=""
            ;;
    esac

    if [[ ${COMP_CWORD} -eq 1 ]]; then
        COMPREPLY=( $(compgen -W "${commands}" -- ${cur}) )
    elif [[ ${COMP_CWORD} -eq 2 && -n "${subcommands}" ]]; then
        COMPREPLY=( $(compgen -W "${subcommands}" -- ${cur}) )
    elif [[ ${cur} == -* ]]; then
        local opts="--help --tsv --men --season --race --event"
        COMPREPLY=( $(compgen -W "${opts}" -- ${cur}) )
    fi
}
complete -F _biathlon_completion biathlon
"""

ZSH_COMPLETION = """
#compdef biathlon

_biathlon() {
    local -a commands subcommands
    commands=(
        'seasons:List available seasons'
        'events:List events'
        'results:Show race results'
        'cumulate:Cumulative statistics'
        'record:Record lists'
        'standings:Cup standings'
        'ceremony:Medal ranking'
        'biathlete:Biathlete information'
        'shooting:Shooting accuracy'
        'relay:Relay race results'
    )

    _arguments -C \\
        '1: :->command' \\
        '*: :->args'

    case $state in
        command)
            _describe 'command' commands
            ;;
        args)
            case $words[2] in
                results)
                    _values 'subcommand' remontada ski range shooting
                    ;;
                cumulate)
                    _values 'subcommand' course ski range shooting miss remontada
                    ;;
                record)
                    _values 'subcommand' lap
                    ;;
                biathlete)
                    _values 'subcommand' info results
                    ;;
            esac
            ;;
    esac
}

_biathlon "$@"
"""


def print_completion(shell: str) -> int:
    """Print shell completion script and exit."""
    if shell == "bash":
        print(BASH_COMPLETION.strip())
    elif shell == "zsh":
        print(ZSH_COMPLETION.strip())
    else:
        print(f"Unknown shell: {shell}. Use 'bash' or 'zsh'.", file=sys.stderr)
        return 1
    return 0


def add_output_flag(subparser: argparse.ArgumentParser) -> None:
>>>>>>> bd1e6ea (modification in cli to support markdown output)
    """Add output-related flags to a subparser."""
    subparser.add_argument(
        "--tsv",
        action="store_true",
        help="Output TSV instead of aligned table (legacy)",
    )
    subparser.add_argument(
        "--format",
        "-f",
        choices=["table", "tsv", "markdown"],
        default="table",
        help="Output format: table (default), tsv, or markdown",
    )
    subparser.add_argument(
        "--output",
        "-o",
        default="",
        help="Write output to file (default: stdout)",
    )
    subparser.add_argument(
        "--columns",
        "-C",
        default="",
        help="Comma-separated list of column names to include (in this order) when using --format markdown",
    )


def _tsv_to_markdown(tsv_text: str, columns: list[str] | None = None) -> str:
    """Convert TSV text (headers in first line) into a Markdown table."""
    lines = [ln for ln in tsv_text.splitlines() if ln.strip()]
    if not lines:
        return ""
    headers = [h.strip() for h in lines[0].split("\t")]
    rows = [[cell.strip() for cell in ln.split("\t")] for ln in lines[1:]]
    if columns:
        requested = [c.strip() for c in columns if c.strip()]
        indices = [headers.index(name) for name in requested if name in headers]
        headers = [headers[i] for i in indices]
        rows = [[r[i] for i in indices] for r in rows]
    return to_markdown_table(headers, rows)


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
<<<<<<< HEAD
    seasons_parser._optionals.title = "optional parameters"
    seasons_parser.add_argument(
        "--limit",
        metavar="INT",
=======
    seasons_parser.add_argument(
        "--limit",
        "-n",
>>>>>>> bd1e6ea (modification in cli to support markdown output)
        type=int,
        default=25,
        help="Limit output rows (default: 25, 0 for all)",
    )
<<<<<<< HEAD
    add_output_format_arg(seasons_parser)
=======
    add_output_flag(seasons_parser)
>>>>>>> bd1e6ea (modification in cli to support markdown output)
    seasons_parser.set_defaults(func=handle_seasons)

    # --- events ---
    events_parser = subparsers.add_parser(
        "events",
        help="List season events",
        formatter_class=CompactOptionalFormatter,
        add_help=False,
    )
<<<<<<< HEAD
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
=======
    events_parser.add_argument(
        "--season",
        "-s",
        default="",
        help="Season id or 'all' (default: current season)",
    )
    events_parser.add_argument(
        "--level",
        "-l",
        default="1",
        help="Levels: -1=All, 0=Mixed, 1=World Cup (default), 2=IBU Cup, 3=Junior, 4=Other, 5=Regional, 6=Para",
    )
    events_parser.add_argument("--search", default="", help="Filter events by name")
    events_parser.add_argument(
        "--sort",
        default="startdate",
        choices=["startdate", "event", "country"],
        help="Sort order",
    )
    events_parser.add_argument(
        "--completed", action="store_true", help="Only completed events"
>>>>>>> bd1e6ea (modification in cli to support markdown output)
    )
    events_parser.add_argument(
        "--summary", action="store_true", help="Show race-type availability per event"
    )
    events_parser.add_argument(
        "--races", action="store_true", help="Include races under each event"
    )
    events_parser.add_argument(
        "--discipline",
<<<<<<< HEAD
        default="",
        help="Filter races by discipline (individual, sprint, pursuit, mass-start, relay, mixed-relay, single-mixed-relay)",
    )
    add_output_format_arg(events_parser)
=======
        "-d",
        default="",
        choices=[
            "individual",
            "sprint",
            "pursuit",
            "massstart",
            "mass-start",
            "relay",
            "",
        ],
        help="Filter races by discipline",
    )
    add_output_flag(events_parser)
>>>>>>> bd1e6ea (modification in cli to support markdown output)
    events_parser.set_defaults(func=handle_events)

    # --- results ---
    results_parser = subparsers.add_parser(
        "results",
        help="Show race results",
        formatter_class=CompactOptionalFormatter,
        add_help=False,
    )
    results_parser.add_argument(
<<<<<<< HEAD
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
=======
        "--race",
        "-r",
        default="",
        help="Race id (default: most recent completed World Cup race)",
    )
    results_parser.add_argument(
        "--sort",
        default="",
        help="Sort by column (result, ski, range, penalty, penaltyloopavg, shooting, misses)",
    )
    results_parser.add_argument(
        "--country",
        "-c",
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
        "-n",
        type=int,
        default=25,
        help="Limit output rows (default: 25, 0 for all)",
    )
    add_output_flag(results_parser)
    results_parser.set_defaults(func=handle_results, results_command=None)
    results_sub = results_parser.add_subparsers(
        dest="results_command", title="subcommands", metavar=""
    )
    for group in results_parser._action_groups:
        if any(
            isinstance(action, argparse._SubParsersAction)
            for action in group._group_actions
        ):
            group.title = "subcommands"
            subcommands_group = group
            break
    else:
        subcommands_group = None
    if subcommands_group:
        results_parser._action_groups.remove(subcommands_group)
        for idx, group in enumerate(results_parser._action_groups):
            if group.title == "optional arguments":
                results_parser._action_groups.insert(idx, subcommands_group)
                break
        else:
            results_parser._action_groups.append(subcommands_group)

    results_ski = results_sub.add_parser(
        "ski",
        help="Show ski time details",
        formatter_class=CompactOptionalFormatter,
        add_help=False,
    )
    results_ski.add_argument("--race", "-r", default="", help="Race id")
    results_ski.add_argument("--sort", default="", help="Sort by column")
    results_ski.add_argument(
        "--country",
        "-c",
        default="",
        help="Filter by country code (e.g., FRA, GER, NOR)",
    )
    results_ski.add_argument(
        "--top",
        type=int,
        default=0,
        help="Filter to top N athletes in World Cup standings",
    )
    results_ski.add_argument(
        "--first", type=int, default=0, help="Filter to first N finishers in the race"
    )
    results_ski.add_argument(
        "--limit",
        "-n",
        type=int,
        default=25,
        help="Limit output rows (default: 25, 0 for all)",
    )
    add_output_flag(results_ski)
    results_ski.set_defaults(func=handle_results_ski)

    results_range = results_sub.add_parser(
        "range",
        help="Show range time details",
        formatter_class=CompactOptionalFormatter,
        add_help=False,
    )
    results_range.add_argument("--race", "-r", default="", help="Race id")
    results_range.add_argument("--sort", default="", help="Sort by column")
    results_range.add_argument(
        "--country",
        "-c",
        default="",
        help="Filter by country code (e.g., FRA, GER, NOR)",
    )
    results_range.add_argument(
        "--top",
        type=int,
        default=0,
        help="Filter to top N athletes in World Cup standings",
    )
    results_range.add_argument(
        "--first", type=int, default=0, help="Filter to first N finishers in the race"
    )
    results_range.add_argument(
        "--limit",
        "-n",
        type=int,
        default=25,
        help="Limit output rows (default: 25, 0 for all)",
    )
    add_output_flag(results_range)
    results_range.set_defaults(func=handle_results_range)

    results_shooting = results_sub.add_parser(
        "shooting",
        help="Show shooting time details",
        formatter_class=CompactOptionalFormatter,
        add_help=False,
    )
    results_shooting.add_argument("--race", "-r", default="", help="Race id")
    results_shooting.add_argument("--sort", default="", help="Sort by column")
    results_shooting.add_argument(
        "--country",
        "-c",
        default="",
        help="Filter by country code (e.g., FRA, GER, NOR)",
    )
    results_shooting.add_argument(
        "--top",
        type=int,
        default=0,
        help="Filter to top N athletes in World Cup standings",
    )
    results_shooting.add_argument(
        "--first", type=int, default=0, help="Filter to first N finishers in the race"
    )
    results_shooting.add_argument(
        "--limit",
        "-n",
        type=int,
        default=25,
        help="Limit output rows (default: 25, 0 for all)",
    )
    add_output_flag(results_shooting)
    results_shooting.set_defaults(func=handle_results_shooting)

    results_remontada = results_sub.add_parser(
        "remontada",
        help="Show pursuit gains (for pursuit races only)",
        formatter_class=CompactOptionalFormatter,
        add_help=False,
    )
    results_remontada.add_argument("--race", "-r", default="", help="Race id")
    results_remontada.add_argument(
        "--country",
        "-c",
        default="",
        help="Filter by country code (e.g., FRA, GER, NOR)",
    )
    results_remontada.add_argument(
>>>>>>> bd1e6ea (modification in cli to support markdown output)
        "--top",
        type=int,
        default=0,
        help="Filter to top N athletes in World Cup standings",
    )
<<<<<<< HEAD
    results_parser.add_argument(
        "--first", type=int, default=0, help="Filter to first N finishers in the race"
    )
    results_parser.add_argument(
        "--limit",
=======
    results_remontada.add_argument(
        "--first", type=int, default=0, help="Filter to first N finishers in the race"
    )
    results_remontada.add_argument(
        "--limit",
        "-n",
>>>>>>> bd1e6ea (modification in cli to support markdown output)
        type=int,
        default=25,
        help="Limit output rows (default: 25, 0 for all)",
    )
<<<<<<< HEAD
    results_parser.add_argument(
=======
    results_remontada.add_argument(
>>>>>>> bd1e6ea (modification in cli to support markdown output)
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
<<<<<<< HEAD

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

=======
    add_cumulate_args(cumulate_parser)
    cumulate_parser.set_defaults(func=handle_cumulate_course, cumulate_command=None)
    cumulate_sub = cumulate_parser.add_subparsers(
        dest="cumulate_command", title="subcommands", metavar=""
    )

    cumulate_course = cumulate_sub.add_parser(
        "course",
        help="Cumulated course times (default)",
        formatter_class=CompactOptionalFormatter,
        add_help=False,
    )
    add_cumulate_args(cumulate_course)
    cumulate_course.set_defaults(func=handle_cumulate_course)

>>>>>>> bd1e6ea (modification in cli to support markdown output)
    cumulate_ski = cumulate_sub.add_parser(
        "ski",
        help="Cumulated ski times",
        formatter_class=CompactOptionalFormatter,
        add_help=False,
    )
<<<<<<< HEAD
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

=======
    add_cumulate_args(cumulate_ski)
    cumulate_ski.set_defaults(func=handle_cumulate_ski)

    cumulate_penalty = cumulate_sub.add_parser(
        "penalty",
        help="Cumulated penalty times",
        formatter_class=CompactOptionalFormatter,
        add_help=False,
    )
    add_cumulate_args(cumulate_penalty)
    cumulate_penalty.set_defaults(func=handle_cumulate_penalty)

>>>>>>> bd1e6ea (modification in cli to support markdown output)
    cumulate_range = cumulate_sub.add_parser(
        "range",
        help="Cumulated range times",
        formatter_class=CompactOptionalFormatter,
        add_help=False,
    )
<<<<<<< HEAD
    cumulate_help["range"] = "Cumulated range times"
    add_cumulate_args(cumulate_range, allow_discipline_event=True)
=======
    add_cumulate_args(cumulate_range)
>>>>>>> bd1e6ea (modification in cli to support markdown output)
    cumulate_range.set_defaults(func=handle_cumulate_range)

    cumulate_shooting = cumulate_sub.add_parser(
        "shooting",
        help="Cumulated shooting times",
        formatter_class=CompactOptionalFormatter,
        add_help=False,
    )
<<<<<<< HEAD
    cumulate_help["shooting"] = "Cumulated shooting times"
    add_cumulate_args(cumulate_shooting, allow_discipline_event=True)
=======
    cumulate_shooting.add_argument(
        "--sort",
        default="shootingtime",
        choices=["shootingtime", "misses", "accuracy", "position"],
        help="Sort order",
    )
    add_cumulate_args(cumulate_shooting)
>>>>>>> bd1e6ea (modification in cli to support markdown output)
    cumulate_shooting.set_defaults(func=handle_cumulate_shooting)

    cumulate_miss = cumulate_sub.add_parser(
        "miss",
        help="Cumulated misses",
        formatter_class=CompactOptionalFormatter,
        add_help=False,
    )
<<<<<<< HEAD
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

=======
    add_cumulate_args(cumulate_miss)
    cumulate_miss.set_defaults(func=handle_cumulate_miss)

>>>>>>> bd1e6ea (modification in cli to support markdown output)
    cumulate_remontada = cumulate_sub.add_parser(
        "remontada",
        help="Cumulated pursuit gains",
        formatter_class=CompactOptionalFormatter,
        add_help=False,
    )
<<<<<<< HEAD
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
        help="Minimum race participation percentage (0-100, default: 66)",
    )
    cumulate_cleansheet.set_defaults(func=handle_cumulate_cleansheet)
=======
    cumulate_remontada.add_argument("--season", "-s", default="", help="Season id")
    cumulate_remontada.add_argument("--men", action="store_true", help="Show men")
    cumulate_remontada.add_argument(
        "--min-race", type=int, default=0, help="Minimum races"
    )
    cumulate_remontada.add_argument(
        "--top", type=int, default=0, help="Filter to top N WC athletes"
    )
    cumulate_remontada.add_argument(
        "--limit",
        "-n",
        type=int,
        default=25,
        help="Limit output rows (default: 25, 0 for all)",
    )
    add_output_flag(cumulate_remontada)
    cumulate_remontada.set_defaults(func=handle_cumulate_remontada)

    for idx, group in enumerate(cumulate_parser._action_groups):
        if group.title == "subcommands":
            subcommands_group = cumulate_parser._action_groups.pop(idx)
            break
    else:
        subcommands_group = None
    if subcommands_group:
        for idx, group in enumerate(cumulate_parser._action_groups):
            if group.title == "optional arguments":
                cumulate_parser._action_groups.insert(idx, subcommands_group)
                break
        else:
            cumulate_parser._action_groups.append(subcommands_group)

    # --- record ---
    record_parser = subparsers.add_parser(
        "record",
        help="Show records (lap, etc.)",
        formatter_class=CompactOptionalFormatter,
        add_help=False,
    )
    record_parser.add_argument("--event", "-e", default="", help="Event id")
    record_parser.add_argument(
        "--discipline",
        "-d",
        default="",
        choices=["individual", "sprint", "pursuit", "massstart", "mass-start", ""],
        help="Discipline filter",
    )
    record_parser.add_argument("--men", action="store_true", help="Show men")
    add_output_flag(record_parser)
    record_parser.set_defaults(func=handle_record_lap, record_command=None)
    record_sub = record_parser.add_subparsers(
        dest="record_command", title="subcommands", metavar=""
    )

    record_lap = record_sub.add_parser(
        "lap",
        help="Top lap times",
        formatter_class=CompactOptionalFormatter,
        add_help=False,
    )
    record_lap.add_argument("--event", "-e", default="", help="Event id")
    record_lap.add_argument(
        "--discipline",
        "-d",
        default="",
        choices=["individual", "sprint", "pursuit", "massstart", "mass-start", ""],
        help="Discipline filter",
    )
    record_lap.add_argument("--men", action="store_true", help="Show men")
    add_output_flag(record_lap)
    record_lap.set_defaults(func=handle_record_lap)
>>>>>>> bd1e6ea (modification in cli to support markdown output)

    # --- standings ---
    standings_parser = subparsers.add_parser(
        "standings",
<<<<<<< HEAD
        help="Show standings (world cup, IBU Cup, etc.)",
        formatter_class=CompactOptionalFormatter,
        add_help=False,
    )
    standings_parser.add_argument("--season", default="", help="Season id")
    standings_parser.add_argument("--men", action="store_true", help="Show men")
    standings_parser.add_argument(
        "--country",
        action="store_true",
        help="Show country standings (Nations Cup + Relay points)",
    )
    standings_parser.add_argument("--level", default="1", help="Cup level")
    standings_parser.add_argument(
        "--sort",
        default="",
        metavar="",
        help=(
            "Sort by column\n"
            "  - athlete: total/sprint/pursuit/individual/massstart\n"
            "  - country: women-nations/men-nations/women-relay/men-relay/mixed-relay"
        ),
    )
    standings_parser.add_argument(
        "--limit",
=======
        help="Show world cup standing",
        formatter_class=CompactOptionalFormatter,
        add_help=False,
    )
    standings_parser.add_argument("--season", "-s", default="", help="Season id")
    standings_parser.add_argument("--men", action="store_true", help="Show men")
    standings_parser.add_argument("--level", "-l", default="1", help="Cup level")
    standings_parser.add_argument(
        "--sort",
        default="total",
        choices=["total", "sprint", "pursuit", "individual", "massstart"],
        help="Sort by column",
    )
    standings_parser.add_argument(
        "--limit",
        "-n",
>>>>>>> bd1e6ea (modification in cli to support markdown output)
        type=int,
        default=25,
        help="Limit output rows (default: 25, 0 for all)",
    )
<<<<<<< HEAD
    standings_parser.add_argument(
        "--u23",
        "--U23",
        action="store_true",
        dest="u23",
        help="Show U23 athletes only (athlete standings mode)",
    )
    add_output_format_arg(standings_parser)
    standings_parser.set_defaults(func=handle_standings)
=======
    add_output_flag(standings_parser)
    standings_parser.set_defaults(func=handle_scores)
>>>>>>> bd1e6ea (modification in cli to support markdown output)

    # --- ceremony ---
    ceremony_parser = subparsers.add_parser(
        "ceremony",
<<<<<<< HEAD
        help="Show medal standing (default: men+women, World Cup, current season)",
=======
        help="Show medal standing",
>>>>>>> bd1e6ea (modification in cli to support markdown output)
        formatter_class=CompactOptionalFormatter,
        add_help=False,
    )
    ceremony_parser.add_argument(
<<<<<<< HEAD
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
=======
        "--athlete", action="store_true", help="Rank by athlete"
    )
    ceremony_parser.add_argument("--race", "-r", default="", help="Race id")
    ceremony_parser.add_argument("--event", "-e", default="", help="Event id")
    gender_group = ceremony_parser.add_mutually_exclusive_group()
    gender_group.add_argument("--men", action="store_true", help="Show men")
    gender_group.add_argument("--women", action="store_true", help="Show women")
    ceremony_parser.add_argument(
        "--country",
        "-c",
        default="",
        help="Filter by host country (where event is held)",
>>>>>>> bd1e6ea (modification in cli to support markdown output)
    )
    ceremony_parser.add_argument(
        "--search",
        default="",
        help="Filter events by name (e.g., 'annecy', 'holmenkollen')",
    )
<<<<<<< HEAD
    ceremony_parser.add_argument(
        "--season", default="", help="Season id (default: current season)"
    )
    add_output_format_arg(ceremony_parser)
    ceremony_parser.set_defaults(func=handle_ceremony)

    # --- achievements ---
    achievements_parser = subparsers.add_parser(
        "achievements",
        help="Show achievements medal table (default: women athletes, World Cup, current season)",
        formatter_class=CompactOptionalFormatter,
        add_help=False,
    )
    achievements_parser.add_argument(
        "--men", action="store_true", help="Show men (default: women)"
    )
    achievements_parser.add_argument(
        "--country",
        action="store_true",
        help="Rank by country (default: by athlete)",
    )
    achievements_parser.add_argument(
        "--nationality",
        default="",
        help="Filter by nationality code (e.g., FRA, NOR, GER)",
    )
    scope_group = achievements_parser.add_mutually_exclusive_group()
    scope_group.add_argument(
        "--olympics",
        action="store_true",
        help="Use Olympics scope (default: World Cup)",
    )
    scope_group.add_argument(
        "--world",
        action="store_true",
        help="Use World Championships scope (default: World Cup)",
    )
    achievements_parser.add_argument(
        "--season",
        default="",
        help="Season id (default: current season, use 'all' to aggregate all seasons)",
    )
    achievements_parser.add_argument(
        "--limit",
        type=int,
        default=50,
        help="Limit output rows (default: 50, 0 for all)",
    )
    add_output_format_arg(achievements_parser)
    achievements_parser.set_defaults(func=handle_achievements)
=======
    ceremony_parser.add_argument("--season", "-s", default="", help="Season id")
    add_output_flag(ceremony_parser)
    ceremony_parser.set_defaults(func=handle_ceremony)

    # --- biathlete ---
    biathlete_parser = subparsers.add_parser(
        "biathlete",
        help="Show biathlete information",
        formatter_class=CompactOptionalFormatter,
        add_help=False,
    )
    biathlete_parser.add_argument(
        "--id", "-i", default="", help="Athlete IBU id (comma-separated)"
    )
    biathlete_parser.add_argument("--search", "-s", default="", help="Search by name")
    biathlete_parser.add_argument("--season", default="", help="Season id")
    add_output_flag(biathlete_parser)
    biathlete_parser.set_defaults(func=handle_athlete_info, athlete_command=None)
    athlete_sub = biathlete_parser.add_subparsers(
        dest="athlete_command", title="subcommands", metavar=""
    )

    athlete_results = athlete_sub.add_parser(
        "results",
        help="Season race ranks",
        formatter_class=CompactOptionalFormatter,
        add_help=False,
    )
    athlete_results.add_argument("--id", "-i", default="", help="Athlete IBU id")
    athlete_results.add_argument("--search", "-s", default="", help="Search by name")
    athlete_results.add_argument("--season", default="", help="Season id")
    athlete_results.add_argument(
        "--level", type=int, default=0, help="Event level (1-5, 0 for all)"
    )
    athlete_results.add_argument("--ski", action="store_true", help="Use ski time rank")
    add_output_flag(athlete_results)
    athlete_results.set_defaults(func=handle_athlete_results)

    athlete_info = athlete_sub.add_parser(
        "info",
        help="Athlete bio info",
        formatter_class=CompactOptionalFormatter,
        add_help=False,
    )
    athlete_info.add_argument(
        "--id", "-i", default="", help="Athlete IBU id (comma-separated)"
    )
    athlete_info.add_argument("--search", "-s", default="", help="Search by name")
    athlete_info.add_argument("--season", default="", help="Season id")
    athlete_info.add_argument(
        "--level", type=int, default=0, help="Event level (1-5, 0 for all)"
    )
    add_output_flag(athlete_info)
    athlete_info.set_defaults(func=handle_athlete_info)
>>>>>>> bd1e6ea (modification in cli to support markdown output)

    # --- shooting ---
    shooting_parser = subparsers.add_parser(
        "shooting",
        help="Show shooting accuracy",
        formatter_class=CompactOptionalFormatter,
        add_help=False,
    )
<<<<<<< HEAD
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
=======
    shooting_parser.add_argument("--race", "-r", default="", help="Race id")
    shooting_parser.add_argument("--event", "-e", default="", help="Event id")
    shooting_parser.add_argument("--season", "-s", default="", help="Season id")
    shooting_parser.add_argument("--men", action="store_true", help="Show men")
    shooting_parser.add_argument(
        "--all-races", action="store_true", help="Only athletes who started every race"
    )
    shooting_parser.add_argument("--sort", default="accuracy", help="Sort order")
    shooting_parser.add_argument(
        "--min-race", type=int, default=0, help="Minimum races"
>>>>>>> bd1e6ea (modification in cli to support markdown output)
    )
    shooting_parser.add_argument(
        "--top", type=int, default=0, help="Restrict to top N athletes in WC standings"
    )
    shooting_parser.add_argument(
        "--limit",
<<<<<<< HEAD
=======
        "-n",
>>>>>>> bd1e6ea (modification in cli to support markdown output)
        type=int,
        default=25,
        help="Limit output rows (default: 25, 0 for all)",
    )
    shooting_parser.add_argument(
        "--debug-races", action="store_true", help="Debug: print races considered"
    )
<<<<<<< HEAD
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
        help="Race analysis (preevent, postevent, preseason, postseason, startlist, postrace)",
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

    # brief preevent
    brief_preevent = brief_sub.add_parser(
        "preevent",
        help="Pre-event agenda and standings snapshot",
        formatter_class=CompactOptionalFormatter,
        add_help=False,
    )
    brief_help["preevent"] = "Pre-event agenda and standings snapshot"
    brief_preevent.add_argument(
        "--event",
=======
    add_output_flag(shooting_parser)
    shooting_parser.set_defaults(func=handle_shooting)

    # --- relay ---
    relay_parser = subparsers.add_parser(
        "relay",
        help="Show relay results",
        formatter_class=CompactOptionalFormatter,
        add_help=False,
    )
    relay_parser.add_argument(
        "--race",
        "-r",
>>>>>>> bd1e6ea (modification in cli to support markdown output)
        default="",
        help="Event id (default: current/in-progress or next level-1 event)",
    )
    add_output_format_arg(brief_preevent)
    brief_preevent.set_defaults(func=handle_brief_preevent)

    # brief postevent
    brief_postevent = brief_sub.add_parser(
        "postevent",
        help="Post-event recap (after an event weekend)",
        formatter_class=CompactOptionalFormatter,
        add_help=False,
    )
<<<<<<< HEAD
    brief_help["postevent"] = "Post-event recap (after an event weekend)"
    brief_postevent.add_argument(
        "--event", default="", help="Event id (default: last completed WC event)"
    )
    brief_postevent.add_argument(
        "--major", action="store_true", help="Use WC+WCH+OWG milestones"
    )
    add_output_format_arg(brief_postevent)
    brief_postevent.set_defaults(func=handle_brief_postevent)

    # brief preseason
    brief_preseason = brief_sub.add_parser(
        "preseason",
        help="Season preview (before the season starts)",
        formatter_class=CompactOptionalFormatter,
        add_help=False,
    )
    brief_help["preseason"] = "Season preview (before the season starts) [WIP]"
    brief_preseason.add_argument(
        "--season", default="", help="Season id (default: current season)"
    )
    brief_preseason.add_argument(
        "--level", default="1", help="Event level (default: 1)"
    )
    add_output_format_arg(brief_preseason)
    brief_preseason.set_defaults(func=handle_brief_preseason)

    # brief postseason
    brief_postseason = brief_sub.add_parser(
        "postseason",
        help="Season summary",
        formatter_class=CompactOptionalFormatter,
        add_help=False,
    )
    brief_help["postseason"] = "Season summary (events and race counts)"
    brief_postseason.add_argument(
        "--season", default="", help="Season id (default: current season)"
    )
    brief_postseason.add_argument(
        "--level", default="1", help="Event level (default: 1)"
    )
    add_output_format_arg(brief_postseason)
    brief_postseason.set_defaults(func=handle_brief_postseason)

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
    add_output_format_arg(brief_startlist)
    brief_startlist.set_defaults(func=handle_brief_startlist)

    # brief postrace
    brief_postrace = brief_sub.add_parser(
        "postrace",
        help="Post-race analysis (after a race)",
        formatter_class=CompactOptionalFormatter,
        add_help=False,
    )
    brief_help["postrace"] = "Post-race analysis (after a race)"
    brief_postrace.add_argument(
        "--race", default="", help="Race id (default: latest completed race)"
    )
    brief_postrace.add_argument(
        "--major", action="store_true", help="Use WC+WCH+OWG milestones"
    )
    add_output_format_arg(brief_postrace)
    brief_postrace.set_defaults(func=handle_brief_postrace)

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
=======
    relay_parser.add_argument(
        "--mixed", action="store_true", help="Show most recent mixed relay"
    )
    relay_parser.add_argument(
        "--singlemixed", action="store_true", help="Show most recent single mixed relay"
    )
    relay_parser.add_argument(
        "--sort", default="", help="Sort by column (e.g., course, penalty, misses)"
    )
    relay_parser.add_argument(
        "--detail",
        action="store_true",
        help="Show leg details (biathlete, result, behind, miss)",
    )
    relay_parser.add_argument(
        "--first", type=int, default=0, help="Filter to first N teams in the race"
    )
    relay_parser.add_argument(
        "--limit",
        "-n",
>>>>>>> bd1e6ea (modification in cli to support markdown output)
        type=int,
        default=25,
        help="Limit output rows (default: 25, 0 for all)",
    )
<<<<<<< HEAD
    form_parser.add_argument(
        "--top", type=int, default=0, help="Filter to top N in WC standings"
    )
    form_parser.add_argument(
        "--nat",
        default="",
        metavar="NATS",
        help="Filter by nationality codes (comma-separated, e.g. FRA,NOR)",
    )
    form_parser.add_argument(
        "--min",
        type=int,
        default=None,
        dest="min_pct",
        metavar="PCT",
        help="Minimum race participation percentage (0-100, default: 50; 0 with --startlist)",
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
=======
    add_output_flag(relay_parser)
    relay_parser.set_defaults(func=handle_relay)
>>>>>>> bd1e6ea (modification in cli to support markdown output)

    return parser


def _tsv_to_markdown(tsv_text: str, columns: list[str] | None = None) -> str:
    """Convert TSV text (headers in first line) into a Markdown table (optionally filter columns)."""
    lines = [ln for ln in tsv_text.splitlines() if ln.strip()]
    if not lines:
        return ""
    headers = [h.strip() for h in lines[0].split("\t")]
    rows = [[cell.strip() for cell in ln.split("\t")] for ln in lines[1:]]
    if columns:
        requested = [c.strip() for c in columns if c.strip()]
        # preserve order of requested list, skip names not present
        indices = [headers.index(name) for name in requested if name in headers]
        headers = [headers[i] for i in indices]
        rows = [[r[i] for i in indices] for r in rows]
    return to_markdown_table(headers, rows)


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
        if getattr(args, "format", "table") == "markdown":
            args.tsv = True
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                ret = args.func(args)
            tsv_text = buf.getvalue()
            cols = (
                [c.strip() for c in args.columns.split(",")]
                if getattr(args, "columns", "")
                else None
            )
            md = _tsv_to_markdown(tsv_text, columns=cols)
            if getattr(args, "output", ""):
                with open(args.output, "w", encoding="utf-8") as f:
                    f.write(md + "\n")
            else:
                print(md)
            return ret

        if getattr(args, "output", ""):
            with open(args.output, "w", encoding="utf-8") as f:
                with contextlib.redirect_stdout(f):
                    return args.func(args)

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
