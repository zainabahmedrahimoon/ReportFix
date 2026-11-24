"""Regression tests for problems found by hand-testing the running app:
spam not caught / no trust deducted, harassment typos and phrasings not high priority,
staff 'waiting for votes' dead end, and the public board's reporter numbers."""
from datetime import datetime, timedelta, timezone

import pytest

from app import config as C
from app import services, triage
from app.services import BadRequest, Forbidden
from conftest import U, reload


def new_student(conn, name="Judge"):
    return services.get_user(conn, services.create_student(conn, name))


def submit(conn, text, user=None):
    return services.submit_report(conn, user or new_student(conn), text)


def issue(conn, iid):
    return conn.execute("SELECT * FROM issues WHERE id=?", (iid,)).fetchone()


# ======================= spam =======================

SPAM = [
    "abhdhfirogtugth", "aabhehfihrebkthigth", "ashjidbudeuhfnvijrogtgjitu",      # typed into the live app
    "kjdfg lab 3", "lab 3 asdhjk qwpeoi zmxnc", "hjsdf hdsjf in library",          # junk + a real place
    "lol this app sucks", "aaaaaaaaaaa", "I am bored", "nothing is wrong just checking", "blah blah blah",
    "hello there", "asdfghjkl", "library", "hahahahaha lab 2", "everything is fine in lab 3", "kuch nahi bas aise hi",
]


@pytest.mark.parametrize("text", SPAM)
def test_spam_rejected_and_trust_deducted(conn, text):
    s = new_student(conn)
    r = submit(conn, text, s)
    assert r.state == "rejected_spam", r.message
    assert reload(conn, s)["trust"] == 45
    assert "trust points deducted" in r.message
    assert conn.execute("SELECT COUNT(*) FROM issues").fetchone()[0] == 8          # nothing routed
    assert conn.execute("SELECT COUNT(*) FROM flags WHERE kind LIKE 'spam%'").fetchone()[0] == 1
    ev = conn.execute("SELECT reason FROM trust_events WHERE user_id=?", (s["id"],)).fetchone()
    assert ev and "Spam report rejected" in ev[0]                                      # the trust log says why


REAL = [  # real reports from the live DB and variants: Roman Urdu shorthand + typos must never be spam
    "library mn light nae a rhi", "IS dept classroom mn fans kaam nae kr rhe", "Cafeteria mn table clean nae hote",
    "library ka heater nae on hota", "lab 3 ka projector kharab hai", "the door of lab 2 won't lock",
    "Lab 4 strengths of the lights are uneven and flickering", "Cafeteria staff were very rude to students today",
    "washroom near lab 1 stinks", "Lab 2 mein chooha hai", "cafeteria ki chai mein makhi thi",
    "the overridden schedule is wrong in Lecture Hall 1", "AC not working", "the lab computer is broken",
]


@pytest.mark.parametrize("text", REAL)
def test_real_reports_are_not_spam(conn, text):
    s = new_student(conn)
    r = submit(conn, text, s)
    assert r.state != "rejected_spam", r.message
    assert reload(conn, s)["trust"] == 50


def test_repeat_spam_escalates(conn):
    s = new_student(conn)
    costs = []
    for t in ["asdfgh", "hello there", "blah blah blah", "zzzzzz zz"]:
        before = reload(conn, s)["trust"]
        submit(conn, t, s)
        costs.append(before - reload(conn, s)["trust"])
    assert costs == [5, 10, 15, 15]                                    # capped at SPAM_MAX_PENALTY
    kinds = [r[0] for r in conn.execute("SELECT kind FROM flags WHERE kind LIKE 'spam%' ORDER BY id")]
    assert kinds[0] == "spam" and set(kinds[1:]) == {"spam_repeat"}
    top = services.admin_flags(conn, U(conn, "Admin (Registrar)"))
    assert any(f["kind"] == "spam_repeat" and f["severity"] == "high" for f in top)


def test_abuse_costs_more_than_spam(conn):
    s = new_student(conn)
    submit(conn, "you are all idiots", s)
    assert reload(conn, s)["trust"] == 42


def test_spammer_reports_lose_weight(conn):
    s = new_student(conn)
    for t in ["asdfgh", "hello there", "blah blah blah", "zzzzzz zz"]:
        submit(conn, t, s)
    assert reload(conn, s)["trust"] < C.CREDIBLE_MIN_TRUST
    assert services.user_weight(conn, reload(conn, s)) == 0


def test_safety_and_harassment_never_spam(conn):
    assert submit(conn, "aag aag aag").state != "rejected_spam"
    assert submit(conn, "help harrased").state != "rejected_spam"


# ======================= harassment =======================

HARASS = [
    ("i got harrased by a snr in CSE building after classes", "high"),           # typed into the live app
    ("ive been harrassed lately by a snr in EE rooftop.", "high"),
    ("a senior herassed me in lab 2", "high"), ("someone harresed me in library", "high"),
    ("seniors are raging me in the cafeteria", "high"), ("ragingg in lab 3 by seniors", "high"),
    ("a guy keeps following me after class from LH1", "high"), ("he stares at me and passes comments in library", "high"),
    ("eve teasing near the cafeteria", "high"), ("ek larka mujhe pareshan karta hai lab 4 mein", "high"),
    ("senior ched raha hai cafeteria mein", "high"), ("woh mujhe ghoorta hai library mein", "high"),
    ("senior ne mujhe maara cafeteria mein", "critical"), ("a senior slapped me in lab 2", "critical"),
    ("a senior hit me in lab 2", "critical"), ("he touched me in the library", "critical"),
    ("someone sends me pics and threatens me", "critical"), ("seniors threatened me near CSE block", "critical"),
]


@pytest.mark.parametrize("text,pri", HARASS)
def test_harassment_confidential_and_prioritised(conn, text, pri):
    r = submit(conn, text)
    assert r.state == "routed", r.message
    i = issue(conn, r.issue_id)
    assert i["is_confidential"] and i["department"] is None and i["base_priority"] == pri
    flag = conn.execute("SELECT * FROM flags WHERE issue_id=?", (r.issue_id,)).fetchone()
    assert flag and flag["kind"] == ("confidential_critical" if pri == "critical" else "confidential")


@pytest.mark.parametrize("text,pri", HARASS)
def test_harassment_floor_holds_against_llm_saying_low(conn, text, pri):
    proposed = {"department": "Labs", "location": "", "category": "other", "priority": "low", "is_safety": False,
                "is_confidential": False, "reason": "x", "location_valid": True}
    g = triage.apply_guards(text, triage.normalize(text), proposed, "llm")
    assert g.is_confidential and g.priority == pri


@pytest.mark.parametrize("text", [
    "the fan follows a weird pattern in lab 2", "the projector hit the floor in lab 2", "harsh lighting in lab 2",
    "please touch the switch in lab 2 carefully", "comments section of lms broken", "push button of lab 2 lift broken",
])
def test_harassment_false_positives(conn, text):
    r = submit(conn, text)
    assert r.issue_id is None or not issue(conn, r.issue_id)["is_confidential"]


def test_door_hit_me_is_injury_not_harassment(conn):
    i = issue(conn, submit(conn, "the door hit me in lab 3").issue_id)
    assert not i["is_confidential"] and i["base_priority"] == "critical"


def test_confidential_flag_never_names_reporter(conn):
    s = new_student(conn, "Victim Name Zed")
    submit(conn, "a senior harrased me in cafeteria", s)
    flags = services.admin_flags(conn, U(conn, "Admin (Registrar)"))
    conf = [f for f in flags if f["kind"].startswith("confidential")]
    assert conf and all(f["who"] is None for f in conf)
    assert all("Victim" not in f["detail"] for f in flags)
    assert flags[0]["kind"].startswith("confidential")                # harassment cases sit at the top


def test_handled_confidential_case_drops_down_flags(conn):
    admin = U(conn, "Admin (Registrar)")
    services.set_status(conn, admin, 7, "in_progress")
    services.set_status(conn, admin, 7, "resolved")
    f = [x for x in services.admin_flags(conn, admin) if x["issue_id"] == 7][0]
    assert f["done"] and f["severity"] == "low"


# ======================= "waiting for votes" dead end =======================

def _backdate_resolution(conn, iid, hours):
    ts = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")
    conn.execute("UPDATE status_log SET created_at=? WHERE issue_id=? AND to_status='resolved'", (ts, iid))
    conn.commit()


def test_resolved_issue_with_no_votes_auto_verifies(conn):
    _backdate_resolution(conn, 3, C.VOTE_WINDOW_HOURS + 1)
    assert services.sweep_votes(conn) == 1
    assert issue(conn, 3)["status"] == "verified"
    note = conn.execute("SELECT note FROM status_log WHERE issue_id=3 ORDER BY id DESC").fetchone()[0]
    assert "voting window ended" in note


def test_window_closes_reopen_when_disputes_lead(conn):
    services.cast_vote(conn, U(conn, "Ayesha Noor"), 3, "dispute")     # 1.0, below threshold 1.136: still pending
    assert issue(conn, 3)["status"] == "resolved"
    _backdate_resolution(conn, 3, C.VOTE_WINDOW_HOURS + 1)
    services.sweep_votes(conn)
    assert issue(conn, 3)["status"] == "open" and issue(conn, 3)["reopen_count"] == 1
    assert reload(conn, U(conn, "Ayesha Noor"))["trust"] == 53           # dispute upheld


def test_window_not_closed_early(conn):
    _backdate_resolution(conn, 3, C.VOTE_WINDOW_HOURS - 1)
    assert services.sweep_votes(conn) == 0 and issue(conn, 3)["status"] == "resolved"


def test_admin_can_close_voting_now_staff_cannot(conn):
    with pytest.raises(Forbidden):
        services.close_voting(conn, 3, U(conn, "Library Staff"))
    assert services.close_voting(conn, 3, U(conn, "Admin (Registrar)")) == "verified"
    with pytest.raises(BadRequest):
        services.close_voting(conn, 3, U(conn, "Admin (Registrar)"))  # already closed


def test_staff_can_reopen_their_own_resolved_issue(conn):
    services.set_status(conn, U(conn, "Library Staff"), 3, "open", "not actually fixed")
    i = issue(conn, 3)
    assert i["status"] == "open" and i["vote_round"] == 2
    with pytest.raises(Forbidden):
        services.set_status(conn, U(conn, "EE Staff"), 3, "in_progress")


def test_staff_quick_action_is_actionable(conn, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app import config, db, main, seed
    path = str(tmp_path / "w.db")
    monkeypatch.setattr(config, "DB_PATH", path)
    c = db.connect(path); seed.reset(c); c.close()
    with TestClient(main.app) as web:
        lib = db.connect(path).execute("SELECT id FROM users WHERE name='Library Staff'").fetchone()[0]
        web.get(f"/switch/{lib}")
        page = web.get("/staff?status=resolved").text
        assert "Auto-closes in" in page and "Reopen (not fixed yet)" in page
        r = web.post("/issue/3/status", data={"to": "open", "back": "/staff?status=resolved"})
        assert r.status_code == 200 and "status -&gt; open" in r.text
        web.get("/switch/1")
        db.connect(path).execute("UPDATE issues SET status='resolved' WHERE id=3").connection.commit()
        assert "Close voting now" in web.get("/admin").text


# ======================= public board numbers =======================

def test_board_breakdown_counts_each_kind_of_reporter(conn):
    usman = U(conn, "Usman Tariq")                                     # trust 25 -> zero weight
    fresh = new_student(conn, "Fresh")                                 # probation -> half
    submit(conn, "Lab 3 ka AC kharab hai", usman)
    submit(conn, "AC in lab 3 not cooling", fresh)
    row = [i for i in services.public_board(conn) if i["id"] == 2][0]
    assert row["reporters"] == 3 and (row["r_full"], row["r_new"], row["r_zero"]) == (1, 1, 1)
    assert row["weighted"] == pytest.approx(1.5)                       # 1.0 + 0.5 + 0
    assert row["score"] == pytest.approx(35 + row["boost"])


def test_board_never_counts_spam_or_shows_confidential(conn):
    before = {i["id"]: i["reporters"] for i in services.public_board(conn)}
    submit(conn, "kjdfg lab 3")
    submit(conn, "a senior harrased me in lab 3")
    after = {i["id"]: i["reporters"] for i in services.public_board(conn)}
    assert before == after
