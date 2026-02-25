# Biathlon CLI

A CLI to explore data from the IBU biathlon results API at [biathlonresults.com].

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

## Releases

CI runs on every push and pull request to `main`.
Publishing to PyPI and creating a GitHub release only happen when a version tag is pushed.

Create a release:

```bash
git tag v1.5.0
git push origin v1.5.0
```

Version numbers are derived dynamically from Git tags via `setuptools-scm`.

PyPI publishing uses GitHub Actions trusted publishing (`id-token: write`), so the
project must be configured as a trusted publisher on PyPI.

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

Show country standings (Nations Cup + Relay points):

```bash
biathlon standings --country
biathlon standings --country --sort women-relay
biathlon standings --country --sort women-nations
```

Show athlete information:

```bash
biathlon athlete info --search "boe johannes"
biathlon athlete id --search "boe"
biathlon athlete results --id BTFRA12305199301
```

Show medal standings:

```bash
biathlon ceremony
biathlon ceremony --athlete
```

Show achievements medal tables:

```bash
biathlon achievements
biathlon achievements --men
biathlon achievements --country
biathlon achievements --nationality FRA
biathlon achievements --olympics
biathlon achievements --world --season all
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
biathlon form --nat FRA,NOR
```

Season, event and race briefs (preevent agenda + standings snapshot, startlist analysis, post-race recap):

```bash
biathlon brief preseason
biathlon brief postseason
biathlon brief preevent
biathlon brief postevent
biathlon brief startlist
biathlon brief postrace
```

Run without installing:

```bash
python -m biathlon.cli seasons
```

Output formats:

```bash
biathlon results --format tsv
biathlon results --format markdown
```

## License

MIT

[biathlonresults.com]: https://biathlonresults.com
