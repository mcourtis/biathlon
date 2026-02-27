"""Biathlon domain constants."""

# Discipline codes
RELAY_DISCIPLINE = "RL"
SINGLE_MIXED_RELAY_DISCIPLINE = "SR"
INDIVIDUAL_DISCIPLINES = {"SP", "PU", "IN", "MS", "SI"}
# Includes legacy Team events (TM) so historical team races are handled as relay/team.
RELAY_DISCIPLINES = frozenset({"RL", "SR", "MR", "TM"})

# Relay category codes
RELAY_WOMEN_CAT = "SW"
RELAY_MEN_CAT = "SM"
RELAY_MIXED_CAT = "MX"

# Shots per discipline (5 shots per stage)
# Sprint: 2 stages, others: 4 stages
SHOTS_PER_DISCIPLINE = {"SP": 10, "PU": 20, "IN": 20, "MS": 20, "SI": 20}

# Skiing laps per discipline
SKI_LAPS = {"SP": 3, "PU": 5, "IN": 5, "MS": 5, "SI": 3}

# Shooting stages (range visits) per discipline
SHOOTING_STAGES = {"SP": 2, "PU": 4, "IN": 4, "MS": 4, "SI": 4}

# Gender/category mappings
GENDER_TO_CAT = {"women": "SW", "men": "SM"}
CAT_TO_GENDER = {"SW": "women", "SM": "men"}

# Discipline display names
DISCIPLINE_NAMES = {
    "SP": "Sprint",
    "PU": "Pursuit",
    "IN": "Individual",
    "MS": "Mass Start",
    "RL": "Relay",
    "TM": "Team",
    "SR": "Single Mixed Relay",
    "MR": "Mixed Relay",
    "SI": "Short Individual",
}

# Category display names for schedule tables
CATEGORY_DISPLAY_NAMES = {
    "SW": "Women",
    "SM": "Men",
    "MX": "Mixed",
}

# Event type constants
EVENT_TYPE_WC = "WC"
EVENT_TYPE_WCH = "WCH"
EVENT_TYPE_OWG = "OWG"

EVENT_TYPE_LABELS = {
    EVENT_TYPE_WC: "World Cup",
    EVENT_TYPE_WCH: "World Championship",
    EVENT_TYPE_OWG: "Olympic Games",
}
