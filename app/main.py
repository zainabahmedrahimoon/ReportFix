import sqlite3
import html
import json
import os
from contextlib import asynccontextmanager
from urllib.parse import quote

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from . import campus, config, db, llm, seed, services, triage
from .services import BadRequest, Forbidden

TEMPLATES = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))
TEMPLATES.env.filters["fromjson"] = lambda s: json.loads(s) if s else {}

# Every role has its own home page. "/" just sends you to yours.
HOME = {"student": "/student", "staff": "/staff", "admin": "/admin"}


def get_conn():
    conn = db.connect()
    try:
        try:
            services.sweep_votes(conn)   # resolved issues past their voting window close themselves
        except sqlite3.OperationalError:
            pass                         # first start: tables not created yet
        yield conn
    finally:
        conn.close()


@asynccontextmanager
async def lifespan(app):
    conn = db.connect()
    db.init(conn)
    if conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
        seed.seed(conn)
    conn.close()
    yield


app = FastAPI(title="ReportFix", lifespan=lifespan)


def current_user(request: Request, conn):
    uid = request.cookies.get("uid")
    u = services.get_user(conn, int(uid)) if uid and uid.isdigit() else None
    return u or conn.execute("SELECT * FROM users WHERE role='student' ORDER BY id LIMIT 1").fetchone()


def need(me, role: str):
    """Role gate for the per-role pages."""
    if me["role"] != role:
        raise Forbidden(f"That page is for {role} accounts; you are signed in as {me['role']} ({me['name']}).")


def go(url: str, msg: str = ""):
    sep = "&" if "?" in url else "?"
    return RedirectResponse(url + (f"{sep}msg={quote(msg)}" if msg else ""), status_code=303)


def safe_back(url: str) -> str:
    """Only same-site absolute paths, so a form field can't redirect people elsewhere."""
    return url if url.startswith("/") and not url.startswith("//") else ""


def render(request, conn, name, **ctx):
    user = current_user(request, conn)
    users = conn.execute("SELECT * FROM users ORDER BY CASE role WHEN 'admin' THEN 0 WHEN 'staff' THEN 1 ELSE 2 END, id").fetchall()
    provider = llm.get_provider()
    return TEMPLATES.TemplateResponse(request, name, {
        "me": user, "users": users, "msg": request.query_params.get("msg", ""),
        "llm_name": type(provider).__name__ if provider else "none (keyword fallback)", **ctx})


@app.exception_handler(Forbidden)
async def _forbidden(request, exc):
    return HTMLResponse(f"<h3>403 Forbidden</h3><p>{html.escape(str(exc))}</p><a href='/'>back to my page</a>", status_code=403)


@app.exception_handler(BadRequest)
async def _bad(request, exc):
    ref = request.headers.get("referer") or "/"
    return go(ref.split("?")[0], f"Error: {exc}")


# ---------------- judge tools ----------------

@app.get("/switch/{uid}")
def switch(uid: int, conn=Depends(get_conn)):
    u = services.get_user(conn, uid)
    if not u:
        raise BadRequest("no such user")
    r = go(HOME[u["role"]])
    r.set_cookie("uid", str(uid))
    return r


@app.post("/students/new")
def new_student(conn=Depends(get_conn)):
    n = conn.execute("SELECT COUNT(*) FROM users WHERE role='student'").fetchone()[0] + 1
    uid = services.create_student(conn, f"New Student {n}")
    r = go(HOME["student"], f"Created and switched to New Student {n} (trust 50, on probation)")
    r.set_cookie("uid", str(uid))
    return r


@app.post("/reset")
def reset(conn=Depends(get_conn)):
    seed.reset(conn)
    r = go("/", "All data reset to demo seed")
    r.delete_cookie("uid")
    return r


@app.get("/")
def home(request: Request, conn=Depends(get_conn)):
    return go(HOME[current_user(request, conn)["role"]], request.query_params.get("msg", ""))


# ---------------- STUDENT pages ----------------

@app.get("/student", response_class=HTMLResponse)
def student_home(request: Request, conn=Depends(get_conn)):
    me = current_user(request, conn)
    need(me, "student")
    issues = services.my_issues(conn, me)
    to_vote = [i for i in issues if i["status"] == "resolved" and not i["my_vote"]]
    stats = {"open": sum(i["status"] == "open" for i in issues),
             "in_progress": sum(i["status"] == "in_progress" for i in issues),
             "resolved": sum(i["status"] == "resolved" for i in issues),
             "verified": sum(i["status"] == "verified" for i in issues)}
    return render(request, conn, "student_home.html", issues=issues, to_vote=to_vote, stats=stats,
                  pending=services.pending_clarifications(conn, me), loc_map=campus.locations_by_department(), weight=services.user_weight(conn, me),
                  probation=services.is_probation(conn, me))


@app.get("/student/report", response_class=HTMLResponse)
def student_report(request: Request, conn=Depends(get_conn)):
    me = current_user(request, conn)
    need(me, "student")
    return render(request, conn, "student_report.html", pending=services.pending_clarifications(conn, me),
                  locations=list(campus.LOCATIONS), loc_map=campus.locations_by_department())


@app.get("/student/reports", response_class=HTMLResponse)
def student_reports(request: Request, conn=Depends(get_conn)):
    me = current_user(request, conn)
    need(me, "student")
    return render(request, conn, "student_reports.html", reports=services.my_reports(conn, me))


# ---------------- STAFF page ----------------

STAFF_TABS = [("active", "Active"), ("open", "Open"), ("in_progress", "In progress"),
              ("resolved", "Resolved (students checking)"), ("verified", "Verified"), ("all", "Everything")]


@app.get("/staff", response_class=HTMLResponse)
def staff_home(request: Request, conn=Depends(get_conn)):
    me = current_user(request, conn)
    need(me, "staff")
    allq = services.staff_queue(conn, me)
    tab = request.query_params.get("status", "active")
    if tab == "active":
        shown = [i for i in allq if i["status"] in ("open", "in_progress")]
    elif tab == "all":
        shown = allq
    else:
        shown = [i for i in allq if i["status"] == tab]
    counts = {"active": sum(i["status"] in ("open", "in_progress") for i in allq), "all": len(allq)}
    for s in ("open", "in_progress", "resolved", "verified"):
        counts[s] = sum(i["status"] == s for i in allq)
    return render(request, conn, "staff.html", issues=shown, tab=tab, tabs=STAFF_TABS, counts=counts)


# ---------------- ADMIN pages ----------------

@app.get("/admin", response_class=HTMLResponse)
def admin_home(request: Request, conn=Depends(get_conn)):
    me = current_user(request, conn)
    need(me, "admin")
    issues = services.admin_public(conn, me)
    dept = request.query_params.get("dept", "")
    if dept in config.DEPARTMENTS:
        issues = [i for i in issues if i["department"] == dept]
    return render(request, conn, "admin_home.html", issues=issues, stats=services.admin_stats(conn),
                  dept=dept, departments=config.DEPARTMENTS)


@app.get("/admin/confidential", response_class=HTMLResponse)
def admin_confidential(request: Request, conn=Depends(get_conn)):
    me = current_user(request, conn)
    need(me, "admin")
    return render(request, conn, "admin_confidential.html", cases=services.admin_confidential(conn, me))


@app.get("/admin/flags", response_class=HTMLResponse)
def admin_flags(request: Request, conn=Depends(get_conn)):
    me = current_user(request, conn)
    need(me, "admin")
    flags = services.admin_flags(conn, me)
    kinds = {}
    for f in flags:
        kinds[f["kind"]] = kinds.get(f["kind"], 0) + 1
    return render(request, conn, "admin_flags.html", flags=flags, kinds=kinds)


@app.get("/admin/users", response_class=HTMLResponse)
def admin_users(request: Request, conn=Depends(get_conn)):
    me = current_user(request, conn)
    need(me, "admin")
    students = conn.execute("SELECT * FROM users WHERE role='student' ORDER BY trust DESC, id").fetchall()
    rows = [{"u": s, "weight": services.user_weight(conn, s), "probation": services.is_probation(conn, s)} for s in students]
    return render(request, conn, "admin_users.html", students=rows)


@app.get("/admin/summary", response_class=HTMLResponse)
def summary(request: Request, conn=Depends(get_conn)):
    me = current_user(request, conn)
    need(me, "admin")
    text, src = services.admin_summary(conn)
    return render(request, conn, "summary.html", text=text, src=src)


# ---------------- shared pages ----------------

@app.get("/board", response_class=HTMLResponse)
def board(request: Request, conn=Depends(get_conn)):
    me = current_user(request, conn)
    return render(request, conn, "board.html", issues=services.board_for(conn, me), trending=services.trending(conn))


@app.get("/issue/{iid}", response_class=HTMLResponse)
def issue_page(iid: int, request: Request, conn=Depends(get_conn)):
    me = current_user(request, conn)
    issue = conn.execute("SELECT * FROM issues WHERE id=?", (iid,)).fetchone()
    if issue is None or not services.can_view(conn, me, issue):
        raise Forbidden("You can't view this issue")
    d = services._decorate(conn, [issue])[0]
    log = conn.execute("SELECT s.*, u.name AS actor FROM status_log s LEFT JOIN users u ON u.id=s.actor_id "
                       "WHERE issue_id=? ORDER BY s.id", (iid,)).fetchall()
    # Triage details (with guards) for staff/admin; students see their own report only.
    if me["role"] == "student":
        reports = conn.execute("SELECT * FROM reports WHERE issue_id=? AND user_id=?", (iid, me["id"])).fetchall()
    else:
        reports = conn.execute("SELECT * FROM reports WHERE issue_id=?", (iid,)).fetchall()
    affected = conn.execute("SELECT 1 FROM issue_reporters WHERE issue_id=? AND user_id=?", (iid, me["id"])).fetchone()
    my_vote = conn.execute("SELECT vote FROM votes WHERE issue_id=? AND user_id=? AND round=?",
                           (iid, me["id"], issue["vote_round"])).fetchone()
    can_join = (me["role"] == "student" and not affected and not issue["is_confidential"]
                and issue["status"] in ("open", "in_progress", "resolved"))
    return render(request, conn, "issue.html", i=d, log=log, reports=reports, tally=services.tally(conn, iid, issue["vote_round"]),
                  can_manage=services.can_manage(me, issue), affected=bool(affected), my_vote=my_vote, can_join=can_join,
                  departments=[x for x in config.DEPARTMENTS if x != issue["department"]],
                  reporters=services.reporters_for_view(conn, me, issue),
                  anon_reporter=services.anon_id if me["role"] == "admin" else None)


@app.get("/users/{uid}/trust", response_class=HTMLResponse)
def trust_page(uid: int, request: Request, conn=Depends(get_conn)):
    u = services.get_user(conn, uid)
    if u is None or u["role"] != "student":
        raise BadRequest("no such student")
    events = conn.execute("SELECT * FROM trust_events WHERE user_id=? ORDER BY id", (uid,)).fetchall()
    return render(request, conn, "trust.html", u=u, events=events, weight=services.user_weight(conn, u),
                  probation=services.is_probation(conn, u))


# ---------------- actions ----------------

@app.post("/report")
def report(request: Request, text: str = Form(...), conn=Depends(get_conn)):
    me = current_user(request, conn)
    res = services.submit_report(conn, me, text)
    # Always land where the student can SEE what happened to their report.
    return go("/student/report" if res.state == "needs_clarification" else "/student/reports", res.message)


@app.post("/report/{rid}/clarify")
def clarify(rid: int, request: Request, location: str = Form(...), conn=Depends(get_conn)):
    res = services.clarify(conn, current_user(request, conn), rid, location)
    return go("/student/reports", res.message)


@app.post("/issue/{iid}/status")
def status(iid: int, request: Request, to: str = Form(...), note: str = Form(""), back: str = Form(""),
           conn=Depends(get_conn)):
    services.set_status(conn, current_user(request, conn), iid, to, note)
    return go(safe_back(back) or f"/issue/{iid}", f"Issue #{iid}: status -> {to}")


@app.post("/issue/{iid}/transfer")
def transfer(iid: int, request: Request, department: str = Form(...), note: str = Form(""), conn=Depends(get_conn)):
    me = current_user(request, conn)
    services.transfer_issue(conn, me, iid, department, note)
    return go("/staff" if me["role"] == "staff" else f"/issue/{iid}", f"Issue #{iid} transferred to {department}")


@app.post("/issue/{iid}/metoo")
def metoo(iid: int, request: Request, back: str = Form(""), conn=Depends(get_conn)):
    msg = services.join_issue(conn, current_user(request, conn), iid)
    return go(safe_back(back) or f"/issue/{iid}", msg)


@app.post("/issue/{iid}/close-voting")
def close_voting(iid: int, request: Request, back: str = Form(""), conn=Depends(get_conn)):
    outcome = services.close_voting(conn, iid, current_user(request, conn))
    msg = {"reopen": "Voting closed: credible disputes won, issue REOPENED.", "verified": "Voting closed: fix VERIFIED."}[outcome]
    return go(safe_back(back) or f"/issue/{iid}", f"Issue #{iid}: {msg}")


@app.post("/issue/{iid}/vote")
def vote(iid: int, request: Request, vote: str = Form(...), conn=Depends(get_conn)):
    outcome = services.cast_vote(conn, current_user(request, conn), iid, vote)
    msg = {"reopen": "Credible disputes passed the threshold: issue REOPENED.",
           "verified": "Fix verified by affected students.", "pending": "Vote recorded."}[outcome]
    return go(f"/issue/{iid}", msg)


# ---------------- JSON helpers for judges ----------------

@app.post("/api/triage-preview")
def triage_preview(text: str = Form(...)):
    """Dry run: shows what the LLM proposed and which code guards fired. Saves nothing."""
    norm = triage.normalize(text)
    out = llm.llm_triage(norm)
    proposed = out.model_dump() if out else triage.fallback_triage(norm)
    final = triage.apply_guards(text, norm, proposed, "llm" if out else "fallback")
    return JSONResponse({"normalized": norm, "source": "llm" if out else "fallback", "proposed": proposed,
                         "final": final.as_dict()})


@app.get("/api/board")
def api_board(conn=Depends(get_conn)):
    return services.public_board(conn)
