"""Campus location whitelist and deterministic matcher.

The LLM is never trusted to say whether a place exists. Location is resolved
in code from the student's own text (with typo tolerance); if nothing on the
whitelist is found, the report is held for clarification instead of routed.
"""
import difflib
import re
from dataclasses import dataclass

NUM_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10,
    # Roman Urdu
    "ek": 1, "do": 2, "teen": 3, "char": 4, "chaar": 4, "paanch": 5, "panch": 5,
}
_NUM = r"(\d{1,3}|" + "|".join(NUM_WORDS) + r")"


@dataclass(frozen=True)
class Location:
    name: str
    owner: str  # default department for building-level problems


# Numbered families: (canonical prefix, regex for the prefix, valid numbers -> owner)
NUMBERED = [
    ("Lab", r"(?:lab|laab|labb|laboratory)", {1: "Labs", 2: "Labs", 3: "Labs", 4: "Labs", 5: "Labs"}),
    ("Lecture Hall", r"(?:lecture\s*hall|lec\s*hall|lh|lt|class\s*room|classroom)", {1: "CSE", 2: "CSE", 3: "EE", 4: "EE"}),
]

# Named places: canonical -> (owner, aliases). Single-word aliases >= 5 chars get typo tolerance.
NAMED = {
    "Library": ("Library", ["library", "lib", "kutub khana", "kutubkhana", "reading hall"]),
    "Cafeteria": ("Cafeteria", ["cafeteria", "canteen", "cafe", "caf"]),
    "CSE Block": ("CSE", ["cse block", "cse department", "cse dept", "cse building", "cse"]),
    "EE Block": ("EE", ["ee block", "ee department", "ee dept", "ee building"]),
    "IS Block": ("IS", ["is block", "is department", "is dept", "is building"]),
    "Server Room": ("IS", ["server room", "data center", "datacenter"]),
    "IT Office": ("IS", ["it office", "is office", "it helpdesk", "helpdesk", "help desk"]),
    "Seminar Hall": ("CSE", ["seminar hall", "seminar room"]),
}


def all_locations() -> list[Location]:
    out = []
    for prefix, _, nums in NUMBERED:
        for n, owner in nums.items():
            out.append(Location(f"{prefix} {n}", owner))
    for name, (owner, _) in NAMED.items():
        out.append(Location(name, owner))
    return out


LOCATIONS = {loc.name: loc for loc in all_locations()}

DEPARTMENT_ORDER = ["EE", "CSE", "IS", "Library", "Cafeteria", "Labs"]


def locations_by_department() -> dict[str, list[str]]:
    """Picker data: every department -> the places it owns (labs, halls, blocks).
    Derived from each location's owner, so the picker and the router can never disagree."""
    out = {d: [] for d in DEPARTMENT_ORDER}
    for loc in LOCATIONS.values():
        out.setdefault(loc.owner, []).append(loc.name)
    return out


def _to_int(tok: str) -> int | None:
    tok = tok.lower()
    if tok.isdigit():
        return int(tok)
    return NUM_WORDS.get(tok)


@dataclass
class LocationMatch:
    location: Location | None
    unknown_mentions: list[str]  # e.g. ["Lab 9"] - named something that looks like a place but isn't real


# Things that look like a campus place but aren't on the whitelist ("Block C", "Hostel 7", "Gate 2").
# "room" is deliberately not here: "room 204 in the CSE Block" is a normal way to describe a known place.
_GENERIC_PLACE = re.compile(r"\b(block|building|hall|wing|hostel|gate)\s*-?\s*([a-z]|\d{1,4})\b")
_ROOM_PLACE = re.compile(r"\b(room|lab)\s*-?\s*([a-z]|\d{1,4})\b")


def match_location(text: str) -> LocationMatch:
    t = " " + re.sub(r"[^a-z0-9#\s-]", " ", text.lower()) + " "
    found: list[tuple[int, Location]] = []
    unknown: list[str] = []
    spans: list[tuple[int, int]] = []  # parts of the text already explained by a known place

    for prefix, pat, nums in NUMBERED:
        rx = re.compile(r"\b" + pat + r"\s*(?:no|number|num|#)?\.?\s*-?\s*" + _NUM + r"\b")
        for m in rx.finditer(t):
            spans.append((m.start(), m.end()))
            n = _to_int(m.group(1))
            if n in nums:
                found.append((m.start(), LOCATIONS[f"{prefix} {n}"]))
            else:
                unknown.append(f"{prefix} {m.group(1)}")

    for name, (_, aliases) in NAMED.items():
        for a in sorted(aliases, key=len, reverse=True):
            m = re.search(r"\b" + re.escape(a).replace(r"\ ", r"\s*") + r"\b", t)
            if m:
                found.append((m.start(), LOCATIONS[name]))
                spans.append((m.start(), m.end()))
                break

    if not found:  # typo tolerance on single long words: "liberary", "cafetaria", "canten"
        tokens = re.findall(r"[a-z]{5,}", t)
        for name, (_, aliases) in NAMED.items():
            for a in aliases:
                if " " in a or len(a) < 5:
                    continue
                for i, tok in enumerate(tokens):
                    if difflib.SequenceMatcher(None, tok, a).ratio() >= 0.8:
                        found.append((t.find(tok), LOCATIONS[name]))
                        break

    # Any *other* place-like phrase is unknown even when a real place was also named:
    # "CSE lab 9" must not be silently routed to the CSE Block.
    def overlaps(m):
        return any(m.start() < e and s < m.end() for s, e in spans)

    for m in _GENERIC_PLACE.finditer(t):
        if not overlaps(m):
            unknown.append(f"{m.group(1).title()} {m.group(2).upper()}")
    if not found:
        for m in _ROOM_PLACE.finditer(t):
            if not overlaps(m):
                unknown.append(f"{m.group(1).title()} {m.group(2).upper()}")
    unknown = list(dict.fromkeys(unknown))  # de-duplicate, keep order

    if found:
        found.sort(key=lambda x: x[0])
        # Prefer a specific room over a building alias like "cse" when both appear
        specific = [loc for _, loc in found if loc.name.split()[0] in ("Lab", "Lecture")]
        return LocationMatch(specific[0] if specific else found[0][1], unknown)
    return LocationMatch(None, unknown)


def resolve_location_name(name: str) -> Location | None:
    """Used for the clarification dropdown: exact whitelist name only."""
    return LOCATIONS.get(name)
