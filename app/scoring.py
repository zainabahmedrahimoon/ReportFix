"""Pure functions: weights, crowd priority, vote thresholds. No DB, fully unit-tested."""
import math
from datetime import datetime, timedelta, timezone

from . import config as C


def weight(trust: float, created_at: datetime | None = None, has_validation: bool = True,
           now: datetime | None = None) -> float:
    """Report/vote weight = f(trust). Not credible (< CREDIBLE_MIN_TRUST) => 0.
    Brand-new accounts with no validated history are on probation (x PROBATION_FACTOR)."""
    if trust < C.CREDIBLE_MIN_TRUST:
        return 0.0
    w = min(C.WEIGHT_MAX, max(C.WEIGHT_MIN, trust / 50.0))
    if created_at is not None and not has_validation:
        now = now or datetime.now(timezone.utc)
        if now - created_at < timedelta(hours=C.PROBATION_HOURS):
            w *= C.PROBATION_FACTOR
    return round(w, 3)


def crowd_boost(weighted_reporters: float) -> float:
    return round(min(C.CROWD_CAP, C.CROWD_K * math.log2(1 + max(0.0, weighted_reporters))), 2)


def priority_score(base_priority: str, weighted_reporters: float) -> float:
    return round(C.BASE_SEVERITY[base_priority] + crowd_boost(weighted_reporters), 2)


def vote_outcome(confirm_w: float, dispute_w: float, affected_w: float) -> str:
    """'reopen' | 'verified' | 'pending'. Reopening is deliberately easier than verifying:
    a fake 'resolved' is worse than a slightly late 'verified'."""
    if affected_w <= 0:
        return "pending"
    reopen_needed = min(C.DISPUTE_ABS, C.DISPUTE_FRAC * affected_w)
    if dispute_w > 0 and dispute_w >= reopen_needed and dispute_w >= confirm_w:
        return "reopen"
    if confirm_w > C.CONFIRM_FRAC * affected_w and confirm_w > dispute_w:
        return "verified"
    return "pending"


def clamp_trust(t: float) -> float:
    return max(C.TRUST_MIN, min(C.TRUST_MAX, t))
