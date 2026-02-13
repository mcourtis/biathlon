# Biathlon CLI

A CLI to explore data from the IBU biathlon results API at https://biathlonresults.com.

No external dependencies - pure Python standard library.

## Installation

### From PyPI

```bash
pip install biathlon
```

### From source

```bash
git clone https://github.com/thbtmntgn/biathlon.git
cd biathlon
pip install .
```

### For development

```bash
git clone https://github.com/thbtmntgn/biathlon.git
cd biathlon
pip install -e .
```

## Usage

List available seasons:

```bash
biathlon seasons
```

List World Cup events from the current season:

```bash
biathlon events
```

List World Cup events for a specific season:

```bash
biathlon events --season 2425
```

List IBU Cup events for the current season:

```bash
biathlon events --level 2
```

List events with their races for the current season World Cup:

```bash
biathlon events --races
```

List sprint races for a specific season:

```bash
biathlon events --season 2425 --races --discipline sprint
```

Show results for the most recent World Cup race:

```bash
biathlon results
```

Show results for a specific race id:

```bash
biathlon results --race BT2526SWRLCP01SWSP
```

Show detailed split times for a race:

```bash
biathlon results --race BT2526SWRLCP03SMMS --detail
```

Show World Cup total standings (women, current season by default):

```bash
biathlon standings
```

Show men sprint standings for season 2425:

```bash
biathlon standings --season 2425 --men --sort sprint
```

Show relay results:

```bash
biathlon relay
biathlon relay --men
biathlon relay --mixed
```

Show biathlete information:

```bash
biathlon biathlete info --search "boe johannes"
biathlon biathlete id --search "boe"
biathlon biathlete results --id BTFRA12305199301
```

Show medal standings:

```bash
biathlon ceremony
biathlon ceremony --athlete
```

Cumulate season statistics:

```bash
biathlon cumulate results
biathlon cumulate remontada --men
biathlon cumulate cleansheet
biathlon cumulate cleansheet --sort percentage
```

Shooting accuracy:

```bash
biathlon shooting
biathlon shooting --men
```

Athlete form (course time or shooting across recent races):

```bash
biathlon form
biathlon form --men --shoot
biathlon form --startlist
biathlon form --season --top 10
```

Event/race briefs (venue history, startlist analysis, post-race recap):

```bash
biathlon brief event
biathlon brief season
biathlon brief startlist
biathlon brief postrace
```

Run without installing:

```bash
python -m biathlon.cli seasons
```

## Shell Completion

Enable tab completion for bash:

```bash
eval "$(biathlon --completion bash)"
```

Or add to your `~/.bashrc`:

```bash
source <(biathlon --completion bash)
```

For zsh, add to your `~/.zshrc`:

```zsh
source <(biathlon --completion zsh)
```

## License

MIT
