# ReportFix

Campus problem reporting for CodeQuest 2026. FastAPI + SQLite + Jinja. Runs locally.

```bash
pip install -r requirements.txt
# pick one (or none: keyword fallback works offline)
export ANTHROPIC_API_KEY=...                     # REPORTFIX_MODEL defaults to claude-haiku-5-5
# or: export OPENAI_BASE_URL=http://localhost:11434/v1 REPORTFIX_MODEL=llama3.1   (Ollama, fully local)
uvicorn app.main:app --reload        # http://127.0.0.1:8000
pytest -q                            # 158 tests
```

The judge bar on every page has one-click user switching (no passwords), **+ New student** and **Reset all data**.

## Pages (each role has its own)
| Role | Pages |
|---|---|
| Student | `/student` dashboard (my issues, fixes waiting for my vote) · `/student/report` (form + pick-a-location prompts) · `/student/reports` (**every submission and what became of it**) · `/users/{id}/trust` |
| Staff | `/staff` department queue with tabs (active / open / in progress / resolved / verified), quick actions, **Transfer** to another department |
| Admin | `/admin` overview + stats (public issues only) · `/admin/confidential` · `/admin/flags` · `/admin/users` (students by trust) · `/admin/summary` |
| Everyone | `/board` public board (students get a **+ Me too** button) · `/issue/{id}` (permission-checked) |

`/` redirects to your role's page; opening another role's page gives a 403.
`POST /api/triage-preview` (form field `text`) does a dry run: it shows what the LLM proposed and which code guards fired, and saves nothing.

## Who decides what

| What | Decided by |
|---|---|
| Department, category, priority guess, one-line reason, "same problem?" | LLM (or keyword fallback) |
| Whether the location exists | **Code**: whitelist matcher over the student's own text (`campus.py`) |
| Safety ⇒ critical | **Code**: keyword regex (EN + Roman Urdu), OR'd with LLM; can only raise |
| Non-safety can't be critical | **Code**: capped at high |
| Tone (CAPS, "urgent", "jaldi", !!!) | **Code**: stripped *before* the LLM sees the text, so the inputs are identical |
| Confidential | **Code**: keyword OR LLM, sticky; dept = NULL; filtered out of every query |
| Status, roles, trust, weights, thresholds, counts | **Code only**. The LLM schema has no such fields, and `extra="forbid"` rejects outputs that try to add them |

### Injection defence (layered)
1. Text is wrapped in a random-nonce tag (`<report_ab12cd34>`). Anything that looks like a tag is stripped first.
2. The system prompt says the content is untrusted data.
3. Output must pass a strict pydantic schema (Literal enums, StrictBool, no extra keys). On failure the keyword fallback runs.
4. A code regex detects injection phrasing (EN + Roman Urdu). When it fires, the LLM's priority/safety/confidential values are discarded, keyword triage is used, the report is flagged for admin, and the reporter gets −5 trust.
5. The dedupe `match_id` must be one of the candidate ids that code supplied. Candidates are open, non-confidential issues at the same location.

## Files
```
app/config.py    every threshold and trust delta
app/campus.py    location whitelist + matcher (typos, "lab-3", "lab three", "LH1", "kutub khana")
app/triage.py    normalize, safety/confidential/injection regex, keyword fallback, apply_guards()
app/llm.py       providers (Anthropic / OpenAI-compatible / none), prompts, schemas
app/scoring.py   pure math: weight(trust), crowd boost, vote outcome
app/services.py  submit → guard → clarify/route → dedupe; staff status; votes; trust; visibility queries
app/seed.py      demo users and issues; reset()
app/main.py      routes (one set per role); templates/ = Jinja pages (student_*, staff, admin_*, board, issue)
tests/test_attacks.py    injection, Roman Urdu fire, fake places, SHOUTING, reworded dupes, confidentiality, permissions
tests/test_community.py  weights, crowd cap, voting/reopen, trust-log integrity, spam, brigading
tests/test_fixes_and_pages.py  categorization, fake places, fault-aware dedupe, me-too, transfer, My reports, per-role pages
```

## Schema
`users`, `reports` (each submission, with the triage JSON and the guards that fired), `issues`,
`issue_reporters` (PK issue+user: the distinct affected students), `votes` (unique per issue/user/round),
`trust_events` (delta, reason, trust_after, idempotent `event_key`), `status_log`, `flags`.

## Numbers (config.py)
- **Weight** = 0 if trust < 30, else clamp(trust/50, 0.6, 1.5). New accounts (<24h, nothing validated yet) count ×0.5.
- **Priority score** = base (critical 100 / high 60 / medium 35 / low 15) + min(25, 8·log2(1 + Σ weights of distinct reporters)).
  A lone trust-100 student on a low issue scores 25.6, which is below a lone medium (35). No crowd can lift low above high, or anything above critical.
- **Reopen** when credible dispute weight ≥ min(2.0, 0.34 × affected weight) **and** ≥ confirm weight. **Verified** when confirms > 50% of affected weight.
- **Trust**: +5 report accepted by staff, +2 me-too accepted, −15 / −5 rejected as fake, +3 dispute upheld, −4 confirmed a fake fix,
  +2 confirm upheld, −2 dispute outweighed, −5 injection, −5 rate-limit spam (>5 reports / 10 min), −5 junk report
  (escalating −10, −15 for repeats within 24h), −8 abuse. Each change is logged once and shown at `/users/{id}/trust`.
- **Voting window**: 48h after "resolved", the issue closes itself: verified, unless credible dispute weight ≥ confirm weight (then reopened).
- **Brigading**: 3+ probation accounts joining or voting on one issue within 10 min ⇒ admin flag.

## What changed in this revision
- **Complaints are always findable.** Submitting lands on *My reports*, which lists every submission with the student's own words and the result (new issue / joined an existing one / waiting for a location). Before, a report that merged into an existing issue showed only someone else's title.
- **Fake places are never routed**, even next to a real one ("CSE lab 9", "Lab 9 and library"). A possible safety report waiting for a location raises an admin flag.
- **Better categorization (keyword fallback):** topic-based keywords in English + Roman Urdu; ties go to the more specific category; "fire extinguisher expired" is safety, not food. A code guard forces department to agree with category (wifi is always IS, AC/lights EE, food Cafeteria, lab equipment Labs) even if the LLM says otherwise.
- **Fault-aware dedupe (fallback):** Lab 2 fan and Lab 2 light stay separate issues; reworded reports of the same fault still merge.
- **Injection detector no longer fires on ordinary words** ("has an air conditioner").
- **Transfer instead of reject:** staff can move a mis-routed issue to the right department with no trust penalty (logged as a `transfer` flag so mis-routing is visible).
- **+ Me too button** on the board and issue page.

## Fixes from hand-testing (spam, harassment, votes, board)
- **Spam is caught and costs trust.** Rejected before the LLM is called: random letters ("abhdhfirogtugth", "hjsdf hdsjf in library"),
  repeated characters, greetings/tests ("I am bored", "just checking", "everything is fine in lab 3"), and any short text that names
  no fault and says nothing is wrong ("library"). Real Roman Urdu shorthand still passes ("library mn light nae a rhi").
  Repeat junk within 24h costs −5, −10, then −15 each, and raises a **high** `spam_repeat` flag. Safety and harassment text is never treated as spam.
- **Harassment priority.** More misspellings (herassed, harresed, hrassed, ragingg) and phrasings (following me, stares at me,
  passing comments, eve teasing, pareshan karta, ched raha, ghoorta, gaaliyan) are confidential. Physical violence
  (slapped/hit/punched me, *mujhe maara*, thappar), touching, groping and sending pictures are **critical**. Every new case raises
  an anonymous flag that sits at the top of *Flags* until the Admin handles it. "The door hit me" is an injury (critical, public), not harassment.
- **No more "waiting for votes" dead end.** Resolved issues show a live tally and an auto-close countdown. Staff can **Reopen** their own
  resolved issue, and the Admin can **Close voting now** (same rule as the timer).
- **Public board numbers.** "Students affected" = distinct students (report or Me too), split into full weight / new (half) / low-trust (0),
  with the score shown as base + crowd. Spam is never counted, and confidential cases never appear.

## Honest limits
- Tests run on the keyword fallback and on fake LLMs (including "compromised" ones). I haven't run them against a live model, so try a real key before the demo.
- Fallback dedupe is topic-based, not semantic: it separates faults it has keywords for, but cannot tell two different faults apart when neither has a recognised keyword. With an LLM key, the LLM decides.
- Confidential reports are never deduped, and no trust events are written for them, because the trust log is viewable and would otherwise leak that they exist. There is no "designated handler" role; only Admin sees them.
- Harassment text still goes to the LLM for classification. To avoid that, skip the LLM when the confidential keyword fires.
- A merged report can raise an issue's severity (e.g. "AC is now sparking") but never lower it.
- The voting window is checked when someone loads a page (no background job), so an idle app closes overdue votes on the next visit.
- The spam filter is heuristic: a very short, vague but honest report ("lab 2 weird") can be rejected. The message tells the student to say what is broken and where.
- Demo-grade auth: the `uid` cookie is unsigned, `/switch/{id}` is a GET, there is no CSRF token, and trust logs are readable by anyone. All intentional for the judges' no-password requirement; don't deploy as-is.
