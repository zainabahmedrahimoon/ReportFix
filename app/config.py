"""All tunable numbers live here so judges (and tests) can see every threshold."""
import os

DEPARTMENTS = ["EE", "CSE", "IS", "Library", "Cafeteria", "Labs"]
PRIORITIES = ["critical", "high", "medium", "low"]
CATEGORIES = [
    "safety", "electrical", "hvac", "network", "it_equipment", "plumbing",
    "cleanliness", "furniture", "food", "library_service", "harassment", "other",
]

DB_PATH = os.environ.get("REPORTFIX_DB", os.path.join(os.path.dirname(__file__), "..", "reportfix.db"))

# ---- Trust ----
TRUST_START = 50.0
TRUST_MIN, TRUST_MAX = 0.0, 100.0
CREDIBLE_MIN_TRUST = 30.0          # below this, reports/votes carry zero weight
WEIGHT_MIN, WEIGHT_MAX = 0.6, 1.5  # weight = clamp(trust/50) for credible users
PROBATION_HOURS = 24               # brand-new accounts with no validated history...
PROBATION_FACTOR = 0.5             # ...count half

TRUST_DELTAS = {
    "report_validated": +5,     # staff acted on your report
    "metoo_validated": +2,      # staff acted on an issue you joined
    "report_rejected": -15,     # staff rejected your report as fake/spam
    "metoo_rejected": -5,
    "dispute_correct": +3,      # your dispute reopened a fake "resolved"
    "confirm_wrong": -4,        # you confirmed a fix the crowd proved wasn't real
    "confirm_correct": +2,      # your confirmation was upheld (issue verified)
    "dispute_wrong": -2,
    "injection_attempt": -5,    # report contained instructions aimed at the system
    "spam_rate": -5,            # too many reports too fast
    "spam_text": -5,            # gibberish / test message / advertising / repeated word
    "abuse_text": -8,           # insults with no problem described
}
SPAM_MAX_PENALTY = -15          # repeat junk in 24h escalates -5, -10, -15 (never more than this per report)

# ---- Priority ----
BASE_SEVERITY = {"critical": 100, "high": 60, "medium": 35, "low": 15}
CROWD_K = 8.0        # boost = min(CROWD_CAP, CROWD_K * log2(1 + weighted_reporters))
CROWD_CAP = 25.0     # => low can never pass high; nothing non-safety reaches critical

# ---- Voting ----
DISPUTE_ABS = 2.0    # reopen if credible dispute weight >= min(DISPUTE_ABS, DISPUTE_FRAC * affected weight)
DISPUTE_FRAC = 0.34  #   AND dispute weight >= confirm weight
CONFIRM_FRAC = 0.5   # verified when confirm weight > CONFIRM_FRAC * affected weight and > disputes

# ---- Spam / brigading ----
RATE_LIMIT_REPORTS = 5
RATE_LIMIT_WINDOW_MIN = 10
BRIGADE_NEW_ACCOUNTS = 3
BRIGADE_WINDOW_MIN = 10

# ---- Fix verification window ----
VOTE_WINDOW_HOURS = 48   # after this, a resolved issue closes itself: verified unless credible disputes >= confirms

MAX_REPORT_CHARS = 1000
