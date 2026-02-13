"""Tests for constants module."""

from biathlon.constants import (
    RELAY_DISCIPLINE,
    INDIVIDUAL_DISCIPLINES,
    SHOTS_PER_DISCIPLINE,
    SKI_LAPS,
    SHOOTING_STAGES,
    GENDER_TO_CAT,
    CAT_TO_GENDER,
)


class TestDisciplineConstants:
    def test_relay_discipline(self):
        assert RELAY_DISCIPLINE == "RL"

    def test_individual_disciplines_is_set(self):
        assert isinstance(INDIVIDUAL_DISCIPLINES, set)

    def test_individual_disciplines_content(self):
        assert INDIVIDUAL_DISCIPLINES == {"SP", "PU", "IN", "MS", "SI"}

    def test_relay_not_in_individual(self):
        assert "RL" not in INDIVIDUAL_DISCIPLINES


class TestShotsPerDiscipline:
    def test_sprint_shots(self):
        assert SHOTS_PER_DISCIPLINE["SP"] == 10

    def test_pursuit_shots(self):
        assert SHOTS_PER_DISCIPLINE["PU"] == 20

    def test_individual_shots(self):
        assert SHOTS_PER_DISCIPLINE["IN"] == 20

    def test_mass_start_shots(self):
        assert SHOTS_PER_DISCIPLINE["MS"] == 20


class TestSkiLaps:
    def test_sprint_laps(self):
        assert SKI_LAPS["SP"] == 3

    def test_pursuit_laps(self):
        assert SKI_LAPS["PU"] == 5

    def test_individual_laps(self):
        assert SKI_LAPS["IN"] == 5

    def test_mass_start_laps(self):
        assert SKI_LAPS["MS"] == 5


class TestShootingStages:
    def test_sprint_stages(self):
        assert SHOOTING_STAGES["SP"] == 2

    def test_pursuit_stages(self):
        assert SHOOTING_STAGES["PU"] == 4

    def test_individual_stages(self):
        assert SHOOTING_STAGES["IN"] == 4

    def test_mass_start_stages(self):
        assert SHOOTING_STAGES["MS"] == 4


class TestGenderMappings:
    def test_gender_to_cat_women(self):
        assert GENDER_TO_CAT["women"] == "SW"

    def test_gender_to_cat_men(self):
        assert GENDER_TO_CAT["men"] == "SM"

    def test_cat_to_gender_sw(self):
        assert CAT_TO_GENDER["SW"] == "women"

    def test_cat_to_gender_sm(self):
        assert CAT_TO_GENDER["SM"] == "men"

    def test_mappings_are_inverse(self):
        for gender, cat in GENDER_TO_CAT.items():
            assert CAT_TO_GENDER[cat] == gender
