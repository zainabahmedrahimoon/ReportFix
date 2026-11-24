import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import db, llm, seed  # noqa: E402


@pytest.fixture
def conn(tmp_path):
    c = db.connect(str(tmp_path / "t.db"))
    seed.reset(c)
    llm.set_provider(None)  # default: keyword fallback, deterministic
    yield c
    llm.set_provider(None)
    c.close()


def U(conn, name):
    return conn.execute("SELECT * FROM users WHERE name=?", (name,)).fetchone()


def reload(conn, user):
    return conn.execute("SELECT * FROM users WHERE id=?", (user["id"],)).fetchone()


class FakeLLM:
    """Simulates an LLM. `triage` and `dedupe` are callables (report_text) -> dict | str."""

    def __init__(self, triage=None, dedupe=None):
        self.triage, self.dedupe, self.calls = triage, dedupe, []

    def complete(self, system, user):
        self.calls.append((system, user))
        fn = self.dedupe if "SAME underlying problem" in system else self.triage
        if fn is None:
            raise RuntimeError("LLM down")
        out = fn(user)
        return out if isinstance(out, str) else json.dumps(out)


def good_triage(**over):
    base = {"department": "EE", "location": "", "category": "electrical", "priority": "medium",
            "is_safety": False, "is_confidential": False, "reason": "x", "location_valid": True}
    base.update(over)
    return base
