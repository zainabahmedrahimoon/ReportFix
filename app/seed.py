"""Demo data. `reset(conn)` drops everything and reseeds (judge 'Reset all data' button)."""
from datetime import datetime, timedelta, timezone

from . import db
from .config import DEPARTMENTS

AGO = lambda **kw: (datetime.now(timezone.utc) - timedelta(**kw)).isoformat(timespec="seconds")

STUDENTS = [
    # name, [(delta, reason)] - trust is built only from logged events so the UI can explain it
    ("Ali Raza", [(+5, "Report on Lab 2 projector accepted by Labs staff"), (+5, "Report on Library lights accepted by Library staff"),
                  (+3, "Dispute on Cafeteria water cooler upheld (fix wasn't real)"), (+5, "Report on LH1 fan accepted by CSE staff"),
                  (+2, "Confirmation upheld on Lab 4 WiFi"), (+5, "Report on Server Room AC accepted by IS staff"),
                  (+5, "Report on Lab 1 sockets accepted by Labs staff")]),           # 80
    ("Sara Khan", [(+5, "Report on Cafeteria hygiene accepted by Cafeteria staff"), (+5, "Report on Lab 5 PCs accepted by Labs staff"),
                   (+3, "Dispute on Library AC upheld (fix wasn't real)"), (+2, "Me-too on LH3 projector accepted by EE staff")]),  # 65
    ("Bilal Ahmed", []),                                                                # 50
    ("Ayesha Noor", []),                                                                # 50
    ("Hina Malik", [(+2, "Me-too on Lab 3 WiFi accepted by Labs staff")]),                # 52
    ("Usman Tariq", [(-15, "Issue rejected by Cafeteria staff as fake/spam: 'free biryani for everyone'"),
                     (-5, "Report contained instructions aimed at the system (ignored)"),
                     (-5, "Spam: more than 5 reports in 10 minutes")]),                 # 25 -> not credible
]


def reset(conn) -> None:
    db.drop_all(conn)
    db.init(conn)
    seed(conn)


def seed(conn) -> None:
    created = AGO(days=30)
    conn.execute("INSERT INTO users(name,role,trust,created_at) VALUES ('Admin (Registrar)','admin',50,?)", (created,))
    for d in DEPARTMENTS:
        conn.execute("INSERT INTO users(name,role,department,trust,created_at) VALUES (?,?,?,50,?)",
                     (f"{d} Staff", "staff", d, created))
    sid = {}
    for name, events in STUDENTS:
        uid = conn.execute("INSERT INTO users(name,role,trust,created_at) VALUES (?,?,50,?)", (name, "student", created)).lastrowid
        sid[name] = uid
        t = 50.0
        for k, (delta, reason) in enumerate(events):
            t = max(0, min(100, t + delta))
            conn.execute("INSERT INTO trust_events(user_id,delta,reason,trust_after,created_at,event_key) VALUES (?,?,?,?,?,?)",
                         (uid, delta, reason, t, AGO(days=25 - k), f"seed:{uid}:{k}"))
        conn.execute("UPDATE users SET trust=? WHERE id=?", (t, uid))

    def issue(title, loc, dept, cat, pri, status, reporters, safety=0, conf=0, reason="", hours=6):
        ts = AGO(hours=hours)
        iid = conn.execute("INSERT INTO issues(title,location,department,category,base_priority,is_safety,is_confidential,"
                           "status,reason,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                           (title, loc, dept, cat, pri, safety, conf, status, reason, ts, ts)).lastrowid
        for j, name in enumerate(reporters):
            rid = conn.execute("INSERT INTO reports(user_id,raw_text,state,issue_id,triage_source,created_at) VALUES (?,?,?,?,?,?)",
                               (sid[name], title, "routed" if j == 0 else "merged", iid, "seed", ts)).lastrowid
            conn.execute("INSERT INTO issue_reporters(issue_id,user_id,report_id,kind,created_at) VALUES (?,?,?,?,?)",
                         (iid, sid[name], rid, "original" if j == 0 else "me_too", AGO(hours=hours - j * 0.5)))
        conn.execute("INSERT INTO status_log(issue_id,from_status,to_status,note,created_at) VALUES (?,?,?,?,?)",
                     (iid, None, "open", "seed", ts))
        if status != "open":
            conn.execute("INSERT INTO status_log(issue_id,from_status,to_status,note,created_at) VALUES (?,?,?,?,?)",
                         (iid, "open", status, "seed", AGO(hours=1)))
        return iid

    issue("Lab 3 wifi bilkul nahi chal raha, internet down since morning", "Lab 3", "Labs", "network", "high", "open",
          ["Ali Raza", "Hina Malik", "Bilal Ahmed"], reason="Whole lab has no internet")
    issue("AC in Lab 3 is not cooling at all", "Lab 3", "Labs", "hvac", "medium", "open", ["Bilal Ahmed"],
          reason="AC not cooling in a lab")
    issue("Library AC still not working, it's really hot on the ground floor", "Library", "Library", "hvac", "medium", "resolved",
          ["Sara Khan", "Ayesha Noor", "Hina Malik"], reason="AC failure in library")
    issue("Cafeteria samosas were stale and the counter is dirty", "Cafeteria", "Cafeteria", "food", "medium", "in_progress",
          ["Ayesha Noor"], reason="Food quality and hygiene")
    issue("Projector in Lab 1 doesn't turn on", "Lab 1", "Labs", "it_equipment", "medium", "open", ["Sara Khan"],
          reason="Projector not working")
    issue("Chair in Lecture Hall 2 is wobbly", "Lecture Hall 2", "CSE", "furniture", "low", "open", ["Ali Raza"],
          reason="Minor furniture issue")
    conf_id = issue("Seniors keep ragging juniors near the CSE Block after evening classes and threatened me", "CSE Block", None,
                    "harassment", "critical", "open", ["Ayesha Noor"], conf=1,
                    reason="Ragging with threats (threats make harassment critical)")
    conn.execute("INSERT INTO flags(kind,issue_id,detail,created_at) VALUES (?,?,?,?)",
                 ("confidential_critical", conf_id, f"new confidential case #{conf_id} (critical) - waiting for the Admin", AGO(hours=6)))
    issue("Server room AC is off and the racks are very hot", "Server Room", "IS", "hvac", "high", "open",
          ["Bilal Ahmed", "Ayesha Noor"], reason="Server room cooling failure")
    conn.commit()
