"""Core business logic. Every permission, count, threshold and trust change is here, in code."""
import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from . import campus, llm, scoring, triage
from . import config as C
from .db import now, parse_ts


class Forbidden(Exception):
    pass


class BadRequest(Exception):
    pass


# ---------------- users / trust ----------------

def get_user(conn, uid) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()


def has_validation(conn, uid) -> bool:
    return conn.execute("SELECT 1 FROM trust_events WHERE user_id=? AND delta>0 LIMIT 1", (uid,)).fetchone() is not None


def user_weight(conn, user) -> float:
    return scoring.weight(user["trust"], parse_ts(user["created_at"]), has_validation(conn, user["id"]))


def is_probation(conn, user) -> bool:
    age = datetime.now(timezone.utc) - parse_ts(user["created_at"])
    return age < timedelta(hours=C.PROBATION_HOURS) and not has_validation(conn, user["id"])


def add_trust(conn, user_id: int, kind: str, reason: str, issue_id=None, key: str | None = None,
              delta: float | None = None) -> bool:
    """Idempotent via event_key. Returns True if applied."""
    d = C.TRUST_DELTAS[kind] if delta is None else delta
    u = get_user(conn, user_id)
    if u is None or u["role"] != "student":
        return False
    if issue_id is not None:  # a trust log line naming a confidential case would reveal who was involved
        row = conn.execute("SELECT is_confidential FROM issues WHERE id=?", (issue_id,)).fetchone()
        if row is not None and row[0]:
            return False
    if key and conn.execute("SELECT 1 FROM trust_events WHERE event_key=?", (key,)).fetchone():
        return False
    new = scoring.clamp_trust(u["trust"] + d)
    conn.execute("INSERT INTO trust_events(user_id,delta,reason,issue_id,event_key,trust_after,created_at)"
                 " VALUES (?,?,?,?,?,?,?)", (user_id, d, reason, issue_id, key, new, now()))
    conn.execute("UPDATE users SET trust=? WHERE id=?", (new, user_id))
    return True


def create_student(conn, name: str) -> int:
    cur = conn.execute("INSERT INTO users(name,role,trust,created_at) VALUES (?,?,?,?)",
                       (name, "student", C.TRUST_START, now()))
    conn.commit()
    return cur.lastrowid


def anon_id(user_id: int) -> str:
    return "Reporter-" + hashlib.sha256(f"reportfix-anon-{user_id}".encode()).hexdigest()[:6]


# ---------------- crowd / priority ----------------

def affected(conn, issue_id) -> list[sqlite3.Row]:
    return conn.execute("SELECT u.*, ir.kind, ir.created_at AS joined_at FROM issue_reporters ir "
                        "JOIN users u ON u.id=ir.user_id WHERE ir.issue_id=? ORDER BY ir.created_at",
                        (issue_id,)).fetchall()


def crowd(conn, issue_id) -> tuple[int, float]:
    rows = affected(conn, issue_id)
    return len(rows), round(sum(user_weight(conn, u) for u in rows), 3)


def reporter_breakdown(conn, issue_id) -> dict:
    """Who is behind the 'students affected' number. Counts only, never names."""
    full = half = zero = 0
    for u in affected(conn, issue_id):
        w = user_weight(conn, u)
        if w == 0:
            zero += 1
        elif is_probation(conn, u):
            half += 1
        else:
            full += 1
    return {"r_full": full, "r_new": half, "r_zero": zero}


def score(conn, issue) -> float:
    _, w = crowd(conn, issue["id"])
    return scoring.priority_score(issue["base_priority"], w)


def _decorate(conn, issues) -> list[dict]:
    out = []
    for i in issues:
        n, w = crowd(conn, i["id"])
        d = dict(i)
        d.update(reporters=n, weighted=w, score=scoring.priority_score(i["base_priority"], w),
                 boost=scoring.crowd_boost(w))
        d["tally"] = tally(conn, i["id"], i["vote_round"]) if i["status"] == "resolved" else None
        if d["tally"]:
            dl = vote_deadline(conn, i["id"])
            d["hours_left"] = max(0, round((dl - datetime.now(timezone.utc)).total_seconds() / 3600, 1)) if dl else None
        d.update(reporter_breakdown(conn, i["id"]))
        out.append(d)
    return sorted(out, key=lambda d: (-d["score"], d["id"]))


# ---------------- report submission ----------------

@dataclass
class SubmitResult:
    state: str
    message: str
    issue_id: int | None = None
    report_id: int | None = None
    triage: dict | None = None


def _flag(conn, kind, detail, issue_id=None, report_id=None):
    conn.execute("INSERT INTO flags(kind,issue_id,report_id,detail,created_at) VALUES (?,?,?,?,?)",
                 (kind, issue_id, report_id, detail, now()))


def submit_report(conn, user, text: str) -> SubmitResult:
    if user is None or user["role"] != "student":
        raise Forbidden("Only students submit reports")
    text = (text or "").strip()
    if len(text) < 5:
        raise BadRequest("Report is too short")
    text = text[:C.MAX_REPORT_CHARS]

    since = (datetime.now(timezone.utc) - timedelta(minutes=C.RATE_LIMIT_WINDOW_MIN)).isoformat(timespec="seconds")
    recent = conn.execute("SELECT COUNT(*) FROM reports WHERE user_id=? AND created_at>=?",
                          (user["id"], since)).fetchone()[0]
    if recent >= C.RATE_LIMIT_REPORTS:
        rid = conn.execute("INSERT INTO reports(user_id,raw_text,state,created_at) VALUES (?,?,?,?)",
                           (user["id"], text, "rejected_spam", now())).lastrowid
        bucket = datetime.now(timezone.utc).strftime("%Y%m%d%H") + str(datetime.now(timezone.utc).minute // C.RATE_LIMIT_WINDOW_MIN)
        add_trust(conn, user["id"], "spam_rate", f"Spam: more than {C.RATE_LIMIT_REPORTS} reports in "
                  f"{C.RATE_LIMIT_WINDOW_MIN} minutes", key=f"spam:{user['id']}:{bucket}")
        _flag(conn, "spam", f"user {user['id']} rate-limited", report_id=rid)
        conn.commit()
        return SubmitResult("rejected_spam", "Too many reports in a short time. Please wait a few minutes.", report_id=rid)

    junk = kind = None
    if not (triage.safety_hit(text) or triage.confidential_hit(text)):   # a real emergency is never "spam"
        if triage.abuse_hit(text) and not campus.match_location(text).location and not triage.topics(triage.normalize(text)):
            junk, kind = "abusive language and no problem described", "abuse_text"   # swearing WITH a real problem is fine
        else:
            junk, kind = triage.spam_reason(text), "spam_text"
    if junk:
        # Repeat offenders pay more: -5, -10, -15 (cap) for the 1st/2nd/3rd+ junk report within 24h.
        day_ago = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(timespec="seconds")
        prior = conn.execute("SELECT COUNT(*) FROM trust_events WHERE user_id=? AND created_at>=? AND "
                             "(event_key LIKE 'spamtxt:%' OR event_key LIKE 'spam:%')", (user["id"], day_ago)).fetchone()[0]
        delta = max(C.SPAM_MAX_PENALTY, C.TRUST_DELTAS[kind] * (1 + prior))
        rid = conn.execute("INSERT INTO reports(user_id,raw_text,state,triage_json,triage_source,created_at) VALUES (?,?,?,?,?,?)",
                           (user["id"], text, "rejected_spam", json.dumps({"reason": f"Rejected: {junk}", "guards": []}),
                            "code", now())).lastrowid
        label = "Abusive" if kind == "abuse_text" else "Spam"
        add_trust(conn, user["id"], kind, f"{label} report rejected ({junk})" + (f" - repeat #{prior + 1} in 24h" if prior else ""),
                  key=f"spamtxt:{rid}", delta=delta)
        _flag(conn, ("abuse" if kind == "abuse_text" else "spam") + ("_repeat" if prior else ""),
              f"report rejected ({junk}); {delta:g} trust" + (f"; {prior + 1} junk reports in 24h" if prior else ""), report_id=rid)
        conn.commit()
        return SubmitResult("rejected_spam", f"Rejected: {junk}. {abs(delta):g} trust points deducted"
                            + (" (repeated spam costs more)" if prior else "") + ". Describe the actual problem and where it is.",
                            report_id=rid)

    norm = triage.normalize(text)
    out = llm.llm_triage(norm)
    source = "llm" if out else "fallback"
    proposed = out.model_dump() if out else triage.fallback_triage(norm)
    final = triage.apply_guards(text, norm, proposed, source)
    tri = final.as_dict() | {"source": source, "normalized_text": norm}

    rid = conn.execute(
        "INSERT INTO reports(user_id,raw_text,state,triage_json,triage_source,injection_flag,created_at)"
        " VALUES (?,?,?,?,?,?,?)",
        (user["id"], text, "needs_clarification", json.dumps(tri), source, int(bool(final.injection)), now())).lastrowid

    if final.injection:
        _flag(conn, "injection", f"pattern '{final.injection}'", report_id=rid)
        add_trust(conn, user["id"], "injection_attempt",
                  "Report contained instructions aimed at the system (ignored)", key=f"inj:{rid}")

    if final.is_confidential:
        iid = _create_issue(conn, user, rid, final, text, actor_note="confidential intake")
        # Admin must see new harassment cases at the top of Flags. No name: the flag points at the case only.
        _flag(conn, "confidential_critical" if final.priority == "critical" else "confidential",
              f"new confidential case #{iid} ({final.priority}) - waiting for the Admin", issue_id=iid, report_id=rid)
        conn.commit()
        return SubmitResult("routed", "Received confidentially. Only the Admin can see this report, and your "
                            "identity is hidden from them. It appears on no public board.", iid, rid, tri)

    if final.location is None or final.unknown_locations:
        # No guessing: a place we can't find (even next to one we can, e.g. "CSE lab 9") is never silently routed.
        unk = f" We couldn't find '{final.unknown_locations[0]}' on campus." if final.unknown_locations else \
            " We couldn't tell which campus location this is."
        msg = unk + " Please pick the exact location so we can route it."
        conn.execute("UPDATE reports SET clarify_msg=? WHERE id=?", (msg.strip(), rid))
        if final.is_safety:  # a possible fire must not wait silently in a student's inbox
            _flag(conn, "safety", "possible safety report is waiting for a valid location - check it", report_id=rid)
        conn.commit()
        return SubmitResult("needs_clarification", msg.strip(), None, rid, tri)

    res = _route(conn, user, rid, final, text, norm, tri)
    conn.commit()
    return res


def clarify(conn, user, report_id: int, location_name: str) -> SubmitResult:
    r = conn.execute("SELECT * FROM reports WHERE id=?", (report_id,)).fetchone()
    if r is None or r["user_id"] != user["id"]:
        raise Forbidden("Not your report")
    if r["state"] != "needs_clarification":
        raise BadRequest("This report doesn't need clarification")
    loc = campus.resolve_location_name(location_name)
    if loc is None:
        raise BadRequest("Pick a location from the list")
    tri = json.loads(r["triage_json"])
    dept = triage.department_for(tri["category"], loc)
    final = triage.FinalTriage(
        department=dept, location=loc,
        category=tri["category"], priority=tri["priority"], is_safety=tri["is_safety"],
        is_confidential=False, reason=tri["reason"], injection=tri["injection"],
        unknown_locations=[], guards=tri["guards"] + [f"location set by student clarification: {loc.name}"])
    res = _route(conn, user, report_id, final, r["raw_text"], tri.get("normalized_text") or r["raw_text"])
    conn.execute("UPDATE reports SET triage_json=? WHERE id=?", (json.dumps(final.as_dict() | {"source": tri["source"]}), report_id))
    conn.commit()
    return res


PRI_RANK = {"low": 0, "medium": 1, "high": 2, "critical": 3}


def _create_issue(conn, user, rid, final: triage.FinalTriage, text: str, actor_note: str) -> int:
    ts = now()
    title = " ".join(text.split())[:90]
    iid = conn.execute(
        "INSERT INTO issues(title,location,department,category,base_priority,is_safety,is_confidential,status,"
        "reason,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (title, final.location.name if final.location else None, final.department, final.category, final.priority,
         int(final.is_safety), int(final.is_confidential), "open", final.reason, ts, ts)).lastrowid
    conn.execute("INSERT INTO issue_reporters(issue_id,user_id,report_id,kind,created_at) VALUES (?,?,?,?,?)",
                 (iid, user["id"], rid, "original", ts))
    conn.execute("INSERT INTO status_log(issue_id,actor_id,from_status,to_status,note,created_at) VALUES (?,?,?,?,?,?)",
                 (iid, None, None, "open", actor_note, ts))
    conn.execute("UPDATE reports SET state='routed', issue_id=? WHERE id=?", (iid, rid))
    return iid


def dedupe_candidates(conn, location: str) -> list[dict]:
    """Only OPEN-ish, NON-confidential issues at the SAME location. Confidential issues are never
    candidates, so a public 'me too' can never reveal or attach to one."""
    rows = conn.execute("SELECT id,title,category,status FROM issues WHERE location=? AND is_confidential=0 "
                        "AND status IN ('open','in_progress','resolved') ORDER BY id DESC LIMIT 15",
                        (location,)).fetchall()
    return [dict(r) for r in rows]


def find_duplicate(conn, final: triage.FinalTriage, norm: str) -> tuple[int | None, str]:
    cands = dedupe_candidates(conn, final.location.name)
    if not cands:
        return None, "no open issues at this location"
    answered, mid = llm.llm_same_problem(norm, cands)
    if answered:
        return mid, "LLM same-problem check"
    # Fallback: same category AND not a clearly different fault (Lab 2 fan vs Lab 2 light stay separate).
    mine = triage.topics(norm)
    same = []
    for c in cands:
        if c["category"] != final.category or final.category == "other":
            continue
        theirs = triage.topics(c["title"])
        if not mine or not theirs or (mine & theirs):
            same.append(c)
    return (same[0]["id"], "fallback: same fault at same location") if same else (None, "fallback: no matching fault at this location")


def _route(conn, user, rid, final: triage.FinalTriage, text: str, norm: str, tri: dict | None = None) -> SubmitResult:
    tri = tri or final.as_dict()
    dup, how = find_duplicate(conn, final, norm)
    if dup is not None:
        issue = conn.execute("SELECT * FROM issues WHERE id=?", (dup,)).fetchone()
        conn.execute("UPDATE reports SET state='merged', issue_id=? WHERE id=?", (dup, rid))
        already = conn.execute("SELECT 1 FROM issue_reporters WHERE issue_id=? AND user_id=?", (dup, user["id"])).fetchone()
        if already:
            return SubmitResult("merged", f"You already reported this (issue #{dup}). It still counts once.", dup, rid, tri)
        conn.execute("INSERT INTO issue_reporters(issue_id,user_id,report_id,kind,created_at) VALUES (?,?,?,?,?)",
                     (dup, user["id"], rid, "me_too", now()))
        # Merging can only raise severity (a new report may reveal sparks); never lowers it.
        if PRI_RANK[final.priority] > PRI_RANK[issue["base_priority"]] and (final.is_safety or final.priority != "critical"):
            conn.execute("UPDATE issues SET base_priority=?, is_safety=MAX(is_safety,?), updated_at=? WHERE id=?",
                         (final.priority, int(final.is_safety), now(), dup))
            conn.execute("INSERT INTO status_log(issue_id,actor_id,from_status,to_status,note,created_at) VALUES (?,?,?,?,?,?)",
                         (dup, None, issue["status"], issue["status"], f"priority raised to {final.priority} by me-too report", now()))
        check_brigading(conn, dup, "join")
        extra = " It was marked resolved - you can now confirm or dispute the fix." if issue["status"] == "resolved" else ""
        return SubmitResult("merged", f"Same problem already reported: added your 'me too' to issue #{dup} ({how}).{extra}",
                            dup, rid, tri)
    iid = _create_issue(conn, user, rid, final, text, actor_note=f"routed to {final.department} ({how})")
    return SubmitResult("routed", f"New issue #{iid} routed to {final.department}, priority {final.priority}.", iid, rid, tri)


# ---------------- "me too" button ----------------

def join_issue(conn, user, issue_id: int) -> str:
    """Explicit 'me too' on a public issue (same effect as reporting the same problem in your own words)."""
    if user is None or user["role"] != "student":
        raise Forbidden("Only students can join an issue")
    issue = conn.execute("SELECT * FROM issues WHERE id=?", (issue_id,)).fetchone()
    if issue is None or issue["is_confidential"] or issue["status"] not in ("open", "in_progress", "resolved"):
        raise BadRequest("You can't join this issue")
    if conn.execute("SELECT 1 FROM issue_reporters WHERE issue_id=? AND user_id=?", (issue_id, user["id"])).fetchone():
        raise BadRequest("You are already on this issue")
    since = (datetime.now(timezone.utc) - timedelta(minutes=C.RATE_LIMIT_WINDOW_MIN)).isoformat(timespec="seconds")
    if conn.execute("SELECT COUNT(*) FROM reports WHERE user_id=? AND created_at>=?", (user["id"], since)).fetchone()[0] >= C.RATE_LIMIT_REPORTS:
        raise BadRequest("Too many actions in a short time. Please wait a few minutes.")
    tri = {"department": issue["department"], "location": issue["location"], "category": issue["category"],
           "priority": issue["base_priority"], "is_safety": bool(issue["is_safety"]), "is_confidential": False,
           "reason": "Joined with the 'me too' button", "injection": None, "unknown_locations": [],
           "guards": ["joined an existing issue with the 'me too' button (no text submitted)"], "source": "me_too"}
    rid = conn.execute("INSERT INTO reports(user_id,raw_text,state,issue_id,triage_json,triage_source,created_at)"
                       " VALUES (?,?,?,?,?,?,?)",
                       (user["id"], f"(me too) {issue['title']}", "merged", issue_id, json.dumps(tri), "me_too", now())).lastrowid
    conn.execute("INSERT INTO issue_reporters(issue_id,user_id,report_id,kind,created_at) VALUES (?,?,?,?,?)",
                 (issue_id, user["id"], rid, "me_too", now()))
    check_brigading(conn, issue_id, "join")
    conn.commit()
    return "You joined issue #%d." % issue_id + (" It is marked resolved: you can now confirm or dispute the fix."
                                               if issue["status"] == "resolved" else "")


# ---------------- department transfer (instead of rejecting a mis-routed report) ----------------

def transfer_issue(conn, actor, issue_id: int, new_dept: str, note: str = "") -> None:
    """Staff (or admin) hand an issue to the right department. No trust penalty: the reporter did nothing wrong."""
    issue = conn.execute("SELECT * FROM issues WHERE id=?", (issue_id,)).fetchone()
    if issue is None:
        raise BadRequest("No such issue")
    if not can_manage(actor, issue):
        raise Forbidden("Not your department's issue")
    if issue["is_confidential"]:
        raise BadRequest("Confidential issues are never transferred to a department")
    if new_dept not in C.DEPARTMENTS:
        raise BadRequest("Unknown department")
    if new_dept == issue["department"]:
        raise BadRequest("The issue is already in that department")
    if issue["status"] not in ("open", "in_progress"):
        raise BadRequest("Only open or in-progress issues can be transferred")
    ts = now()
    old_dept = issue["department"]
    conn.execute("UPDATE issues SET department=?, status='open', updated_at=? WHERE id=?", (new_dept, ts, issue_id))
    conn.execute("INSERT INTO status_log(issue_id,actor_id,from_status,to_status,note,created_at) VALUES (?,?,?,?,?,?)",
                 (issue_id, actor["id"], issue["status"], "open",
                  f"transferred {old_dept} -> {new_dept}" + (f": {note[:120]}" if note else ""), ts))
    _flag(conn, "transfer", f"issue #{issue_id} moved {old_dept} -> {new_dept} by {actor['name']} (AI routing was off)", issue_id=issue_id)
    conn.commit()


# ---------------- staff workflow ----------------

STAFF_TRANSITIONS = {("open", "in_progress"), ("in_progress", "resolved"), ("open", "rejected"), ("resolved", "open")}
ADMIN_TRANSITIONS = STAFF_TRANSITIONS | {("open", "resolved"), ("resolved", "verified"), ("resolved", "open"), ("rejected", "open"),
                                         ("in_progress", "open"), ("in_progress", "rejected"), ("verified", "open")}


def can_manage(user, issue) -> bool:
    if user is None:
        return False
    if user["role"] == "admin":
        return True
    return (user["role"] == "staff" and not issue["is_confidential"]
            and issue["department"] is not None and issue["department"] == user["department"])


def set_status(conn, actor, issue_id: int, new: str, note: str = "") -> None:
    issue = conn.execute("SELECT * FROM issues WHERE id=?", (issue_id,)).fetchone()
    if issue is None:
        raise BadRequest("No such issue")
    if not can_manage(actor, issue):
        raise Forbidden("Not your department's issue")
    old = issue["status"]
    allowed = ADMIN_TRANSITIONS if actor["role"] == "admin" else STAFF_TRANSITIONS
    if (old, new) not in allowed:
        raise BadRequest(f"Transition {old} -> {new} not allowed")
    ts = now()
    round_ = issue["vote_round"]
    if new == "open" and old in ("resolved", "verified"):
        conn.execute("UPDATE issues SET reopen_count=reopen_count+1, vote_round=vote_round+1 WHERE id=?", (issue_id,))
    conn.execute("UPDATE issues SET status=?, updated_at=? WHERE id=?", (new, ts, issue_id))
    conn.execute("INSERT INTO status_log(issue_id,actor_id,from_status,to_status,note,created_at) VALUES (?,?,?,?,?,?)",
                 (issue_id, actor["id"], old, new, note[:200], ts))

    if not issue["is_confidential"]:  # no trust events reference confidential issues (they'd leak existence)
        dept = issue["department"]
        if new in ("in_progress", "resolved"):
            for u in affected(conn, issue_id):
                kind = "report_validated" if u["kind"] == "original" else "metoo_validated"
                add_trust(conn, u["id"], kind, f"{'Report' if u['kind']=='original' else 'Me-too'} on issue #{issue_id} "
                          f"accepted by {dept} staff", issue_id, key=f"valid:{issue_id}:{u['id']}")
        if new == "rejected":
            for u in affected(conn, issue_id):
                kind = "report_rejected" if u["kind"] == "original" else "metoo_rejected"
                add_trust(conn, u["id"], kind, f"Issue #{issue_id} rejected by {dept} staff as fake/spam"
                          + (f": {note[:60]}" if note else ""), issue_id, key=f"reject:{issue_id}:{u['id']}:{round_}")
    conn.commit()


# ---------------- verification votes ----------------

def tally(conn, issue_id: int, round_: int) -> dict:
    aff = affected(conn, issue_id)
    aff_w = sum(user_weight(conn, u) for u in aff)
    rows = conn.execute("SELECT v.vote, u.* FROM votes v JOIN users u ON u.id=v.user_id WHERE v.issue_id=? AND v.round=?",
                        (issue_id, round_)).fetchall()
    cw = sum(user_weight(conn, r) for r in rows if r["vote"] == "confirm")
    dw = sum(user_weight(conn, r) for r in rows if r["vote"] == "dispute")
    return {"affected_w": round(aff_w, 3), "confirm_w": round(cw, 3), "dispute_w": round(dw, 3),
            "confirms": sum(r["vote"] == "confirm" for r in rows), "disputes": sum(r["vote"] == "dispute" for r in rows),
            "outcome": scoring.vote_outcome(cw, dw, aff_w),
            "reopen_needed": round(min(C.DISPUTE_ABS, C.DISPUTE_FRAC * aff_w), 3)}


def cast_vote(conn, user, issue_id: int, vote: str) -> str:
    if vote not in ("confirm", "dispute"):
        raise BadRequest("vote must be confirm or dispute")
    if user is None or user["role"] != "student":
        raise Forbidden("Only students vote")
    issue = conn.execute("SELECT * FROM issues WHERE id=?", (issue_id,)).fetchone()
    if issue is None:
        raise BadRequest("No such issue")
    if not conn.execute("SELECT 1 FROM issue_reporters WHERE issue_id=? AND user_id=?", (issue_id, user["id"])).fetchone():
        raise Forbidden("Only students who reported or joined this issue may vote")
    if issue["status"] != "resolved":
        raise BadRequest("Voting is only open while an issue is marked resolved")
    r = issue["vote_round"]
    try:
        conn.execute("INSERT INTO votes(issue_id,user_id,round,vote,created_at) VALUES (?,?,?,?,?)",
                     (issue_id, user["id"], r, vote, now()))
    except sqlite3.IntegrityError:
        raise BadRequest("You already voted on this fix")
    check_brigading(conn, issue_id, "vote")
    t = tally(conn, issue_id, r)
    if t["outcome"] != "pending":
        _apply_outcome(conn, issue_id, r, t["outcome"], t, None)
    conn.commit()
    return t["outcome"]


def _apply_outcome(conn, issue_id: int, r: int, outcome: str, t: dict, note: str | None, actor_id=None) -> None:
    """Reopen or verify a resolved issue and settle the voters' trust. Used by votes AND by the window closing."""
    voters = conn.execute("SELECT user_id, vote FROM votes WHERE issue_id=? AND round=?", (issue_id, r)).fetchall()
    ts = now()
    if outcome == "reopen":
        conn.execute("UPDATE issues SET status='open', reopen_count=reopen_count+1, vote_round=vote_round+1, updated_at=? WHERE id=?",
                     (ts, issue_id))
        conn.execute("INSERT INTO status_log(issue_id,actor_id,from_status,to_status,note,created_at) VALUES (?,?,?,?,?,?)",
                     (issue_id, actor_id, "resolved", "open", note or
                      f"reopened by credible disputes ({t['dispute_w']} >= {t['reopen_needed']}, confirms {t['confirm_w']})", ts))
        for v in voters:
            if v["vote"] == "dispute":
                add_trust(conn, v["user_id"], "dispute_correct", f"Dispute on issue #{issue_id} upheld (fix wasn't real)",
                          issue_id, key=f"vote:{issue_id}:{r}:{v['user_id']}")
            else:
                add_trust(conn, v["user_id"], "confirm_wrong", f"Confirmed a fix on issue #{issue_id} that the crowd proved wasn't real",
                          issue_id, key=f"vote:{issue_id}:{r}:{v['user_id']}")
    elif outcome == "verified":
        conn.execute("UPDATE issues SET status='verified', updated_at=? WHERE id=?", (ts, issue_id))
        conn.execute("INSERT INTO status_log(issue_id,actor_id,from_status,to_status,note,created_at) VALUES (?,?,?,?,?,?)",
                     (issue_id, actor_id, "resolved", "verified", note or
                      f"fix confirmed by affected students ({t['confirm_w']} of {t['affected_w']})", ts))
        for v in voters:
            kind, why = (("confirm_correct", "Confirmation upheld") if v["vote"] == "confirm"
                         else ("dispute_wrong", "Dispute outweighed by credible confirmations"))
            add_trust(conn, v["user_id"], kind, f"{why} on issue #{issue_id}", issue_id, key=f"vote:{issue_id}:{r}:{v['user_id']}")


# ---------------- voting window: "resolved" never waits forever ----------------

def resolved_at(conn, issue_id: int) -> datetime | None:
    row = conn.execute("SELECT MAX(created_at) FROM status_log WHERE issue_id=? AND to_status='resolved'", (issue_id,)).fetchone()
    return parse_ts(row[0]) if row and row[0] else None


def vote_deadline(conn, issue_id: int) -> datetime | None:
    ts = resolved_at(conn, issue_id)
    return ts + timedelta(hours=C.VOTE_WINDOW_HOURS) if ts else None


def close_voting(conn, issue_id: int, actor=None, why: str = "") -> str:
    """End the vote on a resolved issue. Rule (same for the timer and the Admin button):
    credible disputes >= credible confirms (and > 0)  -> reopen;  otherwise -> verified."""
    issue = conn.execute("SELECT * FROM issues WHERE id=?", (issue_id,)).fetchone()
    if issue is None:
        raise BadRequest("No such issue")
    if actor is not None and actor["role"] != "admin":
        raise Forbidden("Only the Admin can close voting early")
    if issue["status"] != "resolved":
        raise BadRequest("Voting is only open while an issue is marked resolved")
    r = issue["vote_round"]
    t = tally(conn, issue_id, r)
    outcome = "reopen" if t["dispute_w"] > 0 and t["dispute_w"] >= t["confirm_w"] else "verified"
    who = f"closed by {actor['name']}" if actor else f"{C.VOTE_WINDOW_HOURS}h voting window ended"
    detail = (f"{who}: confirms {t['confirm_w']} vs disputes {t['dispute_w']}"
              + ("" if t["confirms"] + t["disputes"] else " (no student disputed the fix)") + (f" - {why}" if why else ""))
    _apply_outcome(conn, issue_id, r, outcome, t, detail, actor["id"] if actor else None)
    conn.commit()
    return outcome


def sweep_votes(conn) -> int:
    """Close every resolved issue whose voting window has passed. Cheap; called on page loads."""
    n = 0
    for row in conn.execute("SELECT id FROM issues WHERE status='resolved'").fetchall():
        dl = vote_deadline(conn, row["id"])
        if dl and datetime.now(timezone.utc) >= dl:
            close_voting(conn, row["id"])
            n += 1
    return n


def check_brigading(conn, issue_id: int, what: str) -> bool:
    since = (datetime.now(timezone.utc) - timedelta(minutes=C.BRIGADE_WINDOW_MIN)).isoformat(timespec="seconds")
    if what == "vote":
        rows = conn.execute("SELECT u.* FROM votes v JOIN users u ON u.id=v.user_id WHERE v.issue_id=? AND v.created_at>=?",
                            (issue_id, since)).fetchall()
    else:
        rows = conn.execute("SELECT u.* FROM issue_reporters ir JOIN users u ON u.id=ir.user_id WHERE ir.issue_id=? "
                            "AND ir.created_at>=?", (issue_id, since)).fetchall()
    new = [u for u in rows if is_probation(conn, u)]
    if len(new) >= C.BRIGADE_NEW_ACCOUNTS:
        if not conn.execute("SELECT 1 FROM flags WHERE kind='brigading' AND issue_id=? AND created_at>=? AND detail LIKE ?",
                            (issue_id, since, f"{what}%")).fetchone():
            _flag(conn, "brigading", f"{what}: {len(new)} brand-new accounts acted on this issue within "
                  f"{C.BRIGADE_WINDOW_MIN} min (their weight is halved)", issue_id=issue_id)
        return True
    return False


# ---------------- visibility (the only queries the UI uses) ----------------

def public_board(conn) -> list[dict]:
    rows = conn.execute("SELECT * FROM issues WHERE is_confidential=0 AND status!='rejected'").fetchall()
    return _decorate(conn, rows)


def staff_queue(conn, user) -> list[dict]:
    if user["role"] != "staff":
        raise Forbidden("staff only")
    rows = conn.execute("SELECT * FROM issues WHERE department=? AND is_confidential=0", (user["department"],)).fetchall()
    return _decorate(conn, rows)


def admin_all(conn, user) -> list[dict]:
    if user["role"] != "admin":
        raise Forbidden("admin only")
    return _decorate(conn, conn.execute("SELECT * FROM issues").fetchall())


def my_issues(conn, user) -> list[dict]:
    rows = conn.execute("SELECT i.*, ir.kind AS my_kind FROM issues i JOIN issue_reporters ir ON ir.issue_id=i.id "
                        "WHERE ir.user_id=?", (user["id"],)).fetchall()
    out = _decorate(conn, rows)
    for d in out:
        d["my_vote"] = (conn.execute("SELECT vote FROM votes WHERE issue_id=? AND user_id=? AND round=?",
                                     (d["id"], user["id"], d["vote_round"])).fetchone() or [None])[0]
    return out


def my_reports(conn, user) -> list[dict]:
    """Every submission this student made (including ones merged into someone else's issue and ones still waiting)."""
    rows = conn.execute(
        "SELECT r.*, i.title AS issue_title, i.status AS issue_status, i.department AS issue_dept, i.location AS issue_loc, "
        "i.base_priority AS issue_priority, i.is_confidential AS issue_conf, i.is_safety AS issue_safety "
        "FROM reports r LEFT JOIN issues i ON i.id=r.issue_id WHERE r.user_id=? ORDER BY r.id DESC", (user["id"],)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["tri"] = json.loads(r["triage_json"]) if r["triage_json"] else {}
        out.append(d)
    return out


def pending_clarifications(conn, user) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM reports WHERE user_id=? AND state='needs_clarification' ORDER BY id DESC",
                        (user["id"],)).fetchall()


def board_for(conn, user) -> list[dict]:
    """Public board, plus whether the viewing student already joined each issue (for the 'me too' button)."""
    out = public_board(conn)
    joined = {r[0] for r in conn.execute("SELECT issue_id FROM issue_reporters WHERE user_id=?", (user["id"],))}
    for d in out:
        d["joined"] = d["id"] in joined
    return out


def admin_public(conn, user) -> list[dict]:
    return [i for i in admin_all(conn, user) if not i["is_confidential"]]


def admin_confidential(conn, user) -> list[dict]:
    rows = [i for i in admin_all(conn, user) if i["is_confidential"]]
    # open cases first, then by priority (critical -> low), then longest waiting
    rows.sort(key=lambda d: (d["status"] in ("verified", "rejected"), -PRI_RANK[d["base_priority"]], d["created_at"]))
    for d in rows:
        d["reporter_ids"] = [anon_id(r["id"]) for r in affected(conn, d["id"])]
    return rows


FLAG_SEVERITY = {"safety": "critical", "confidential_critical": "critical", "confidential": "high", "injection": "high", "brigading": "high",
                 "spam_repeat": "high", "abuse_repeat": "high", "spam": "medium", "abuse": "medium", "transfer": "low"}
_SEV_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3}


def admin_flags(conn, user) -> list[dict]:
    """Flags, most urgent first. The student's name is shown for spam/injection, but NEVER when the flagged report
    became a confidential case (that would reveal the reporter)."""
    if user["role"] != "admin":
        raise Forbidden("admin only")
    rows = conn.execute(
        "SELECT f.*, CASE WHEN ri.is_confidential=1 OR fi.is_confidential=1 THEN NULL ELSE u.name END AS who, "
        "CASE WHEN ri.is_confidential=1 OR fi.is_confidential=1 THEN NULL ELSE u.trust END AS who_trust, "
        "COALESCE(fi.status, ri.status) AS issue_status "
        "FROM flags f LEFT JOIN reports r ON r.id=f.report_id LEFT JOIN users u ON u.id=r.user_id "
        "LEFT JOIN issues ri ON ri.id=r.issue_id LEFT JOIN issues fi ON fi.id=f.issue_id ORDER BY f.id DESC LIMIT 300").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["severity"] = FLAG_SEVERITY.get(r["kind"], "low")
        # A confidential case the Admin already handled drops out of the urgent band.
        d["done"] = r["kind"].startswith("confidential") and r["issue_status"] in ("resolved", "verified", "rejected")
        if d["done"]:
            d["severity"] = "low"
        out.append(d)
    return sorted(out, key=lambda d: (d["done"], _SEV_RANK[d["severity"]], -d["id"]))


def admin_stats(conn) -> dict:
    q = lambda sql: conn.execute(sql).fetchone()[0]
    return {
        "open": q("SELECT COUNT(*) FROM issues WHERE is_confidential=0 AND status='open'"),
        "in_progress": q("SELECT COUNT(*) FROM issues WHERE is_confidential=0 AND status='in_progress'"),
        "awaiting_votes": q("SELECT COUNT(*) FROM issues WHERE is_confidential=0 AND status='resolved'"),
        "verified": q("SELECT COUNT(*) FROM issues WHERE is_confidential=0 AND status='verified'"),
        "critical_open": q("SELECT COUNT(*) FROM issues WHERE is_confidential=0 AND base_priority='critical' "
                           "AND status IN ('open','in_progress')"),
        "reopened": q("SELECT COUNT(*) FROM issues WHERE is_confidential=0 AND reopen_count>0"),
        "confidential_open": q("SELECT COUNT(*) FROM issues WHERE is_confidential=1 AND status NOT IN ('verified','rejected')"),
        "flags": q("SELECT COUNT(*) FROM flags"),
    }


def can_view(conn, user, issue) -> bool:
    if user is None:
        return False
    if user["role"] == "admin":
        return True
    if user["role"] == "staff":
        return can_manage(user, issue)
    if issue["is_confidential"]:
        return conn.execute("SELECT 1 FROM issue_reporters WHERE issue_id=? AND user_id=?",
                            (issue["id"], user["id"])).fetchone() is not None
    return issue["status"] != "rejected"


def reporters_for_view(conn, user, issue) -> list[str]:
    """Names only for admin on non-confidential issues; confidential => anonymized always."""
    rows = affected(conn, issue["id"])
    if issue["is_confidential"]:
        return [anon_id(r["id"]) for r in rows] if user["role"] == "admin" else []
    if user["role"] == "admin":
        return [f"{r['name']} ({r['kind']}, trust {r['trust']:.0f})" for r in rows]
    return []


def trending(conn, hours: int = 24) -> list[dict]:
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")
    rows = conn.execute("SELECT i.*, COUNT(ir.user_id) AS recent FROM issues i JOIN issue_reporters ir ON ir.issue_id=i.id "
                        "WHERE i.is_confidential=0 AND i.status IN ('open','in_progress','resolved') AND ir.created_at>=? "
                        "GROUP BY i.id ORDER BY recent DESC, i.id LIMIT 5", (since,)).fetchall()
    return [dict(r) for r in rows]


def admin_summary(conn) -> tuple[str, str]:
    issues = public_board(conn)
    conf = conn.execute("SELECT COUNT(*) FROM issues WHERE is_confidential=1 AND status NOT IN ('verified','rejected')").fetchone()[0]
    lines = [f"#{i['id']} [{i['status']}] {i['base_priority']} score {i['score']} {i['department']} @ {i['location']}: "
             f"{i['title']} ({i['reporters']} reporters, reopened {i['reopen_count']}x)" for i in issues[:30]]
    lines.append(f"Confidential cases open: {conf} (details withheld)")
    ai = llm.llm_admin_summary(lines)  # confidential text is never sent
    if ai:
        return ai, "llm"
    openi = [i for i in issues if i["status"] in ("open", "in_progress")]
    crit = [i for i in openi if i["base_priority"] == "critical"]
    by_dept: dict[str, int] = {}
    for i in openi:
        by_dept[i["department"]] = by_dept.get(i["department"], 0) + 1
    s = [f"- {len(openi)} open issues; {len(crit)} critical safety issue(s)" + (f": " + "; ".join(f"#{i['id']} {i['location']}" for i in crit) if crit else ""),
         "- Open by department: " + ", ".join(f"{k} {v}" for k, v in sorted(by_dept.items(), key=lambda x: -x[1])),
         "- Reopened after a disputed fix: " + (", ".join(f"#{i['id']}" for i in issues if i["reopen_count"]) or "none"),
         "- Top by crowd priority: " + ", ".join(f"#{i['id']} ({i['score']})" for i in openi[:3]),
         f"- Confidential cases open: {conf}"]
    return "\n".join(s), "fallback"
