"""Deterministic triage: normalization, safety/confidential/injection detection,
keyword fallback, and the code guards that sit on top of whatever the LLM says.
"""
import difflib
import re
from dataclasses import dataclass, field

from . import campus
from .config import CATEGORIES, DEPARTMENTS, MAX_REPORT_CHARS

# ---------------- normalization (tone must not matter) ----------------

URGENCY_WORDS = [
    "urgent", "urgently", "urgnt", "urjent", "asap", "immediately", "right now", "right away",
    "emergency please", "plz", "pls", "please", "plzz", "jaldi", "jldi", "foran", "fori", "fauran",
    "turant", "abhi ke abhi", "jaldi karo", "jaldi kar do", "jaldi kardo", "foran karo", "foran kar do", "fix karo", "emergency", "emergancy", "critical", "crucial", "seriously", "important", "very important", "top priority", "high priority",
]


def normalize(text: str) -> str:
    """Lowercase, strip urgency words, collapse repeated punctuation/letters.
    The LLM only ever sees this version, so 'FAN NOISE URGENT!!!' and 'fan noise'
    produce identical input -> identical triage. Invariance by construction."""
    t = text[:MAX_REPORT_CHARS].lower()
    t = re.sub(r"([!?.])\1+", r"\1", t)
    t = re.sub(r"(\w)\1{2,}", r"\1\1", t)        # "plzzzzz" -> "plzz", "noooo" -> "noo"
    for w in sorted(URGENCY_WORDS, key=len, reverse=True):
        t = re.sub(r"\b" + re.escape(w) + r"\b", " ", t)
    t = re.sub(r"[!?,;:]+", " ", t)              # tone punctuation carries no meaning for triage
    t = re.sub(r"\s+\.", ".", t)
    return re.sub(r"\s+", " ", t).strip(" .")


# ---------------- safety (always critical) ----------------

SAFETY_PATTERNS = [
    # fire / smoke
    r"fire", r"fier", r"fyre", r"on fire", r"flames?", r"burning", r"burnt smell", r"burning smell",
    r"smoke", r"smoking socket", r"smok(?:e|ing)", r"aag", r"aag lag", r"dhuan", r"dhuwan", r"dhoowan",
    r"jal raha", r"jal rahi", r"jal rahe", r"jalne", r"jal gaya", r"jal gayi",
    # electrical
    r"sparks?", r"sparking", r"sparkz", r"short circuit", r"shortcircuit", r"electric shock",
    r"electrocut\w*", r"live wire", r"naked wire", r"exposed wires?", r"chingari\w*", r"current lag\w*",
    r"karant", r"current maar\w*", r"shock lag\w*",
    # gas
    r"gas leak\w*", r"smell of gas", r"gas smell", r"gas ki (?:boo|bu|smell)", r"gas leak ho",
    # medical / injury
    r"injur\w*", r"bleeding", r"blood", r"unconscious", r"faint\w*", r"collapsed", r"seizure",
    r"heart attack", r"ambulance", r"medical emergency", r"not breathing", r"can'?t breathe",
    r"behosh", r"khoon", r"zakhmi", r"chot lag\w*", r"gir gaya", r"gir gayi", r"saans nahi",
    r"allergic reaction", r"hit me", r"fell on (?:me|him|her|a student|someone)", r"got hurt", r"hurt (?:his|her|my) (?:head|leg|arm|hand)", r"food poisoning",
]
_SAFETY_RX = re.compile(r"\b(?:" + "|".join(SAFETY_PATTERNS) + r")\b")


_NOT_TYPOS = {"raging", "raged", "rigging", "stacking"}   # real words that sit close to a keyword


def _fuzzy_hit(text: str, vocab: list[str], cutoff: float = 0.88, minlen: int = 6) -> str | None:
    """Typo tolerance for the words that matter most. Only longish tokens, strict cutoff, so ordinary words
    ('stacking', 'training', 'cleaning') do not trigger. 'harrased', 'harasment', 'molsted' do."""
    for tok in re.findall(r"[a-z]+", text.lower()):
        if len(tok) >= minlen and tok not in _NOT_TYPOS and difflib.get_close_matches(tok, vocab, n=1, cutoff=cutoff):
            return tok
    return None


_SAFETY_VOCAB = ["burning", "sparking", "smoking", "electrocuted", "electrocution", "unconscious", "bleeding", "fainted",
                 "explosion", "exploded", "shortcircuit", "ambulance", "injured", "collapsed", "seizure", "extinguisher"]
_SAFETY_TYPO_RX = re.compile(r"\b(?:smoek|somke|smoak|firee|fyer|spaarks?|sprk|gass leak|gas leek)\b")


def safety_hit(text: str) -> str | None:
    m = _SAFETY_RX.search(text.lower()) or _SAFETY_TYPO_RX.search(text.lower())
    if m:
        return m.group(0)
    # fuzzy only for words that are unambiguous hazards; "extinguisher" alone is handled by TOPICS, not here
    tok = _fuzzy_hit(text, [w for w in _SAFETY_VOCAB if w != "extinguisher"])
    return tok


# ---------------- confidential (harassment / ragging) ----------------

# "hit me" only counts as harassment when a person did it ("the door hit me" is an injury, not harassment)
_PERSON_HIT = (r"(?:he|she|they|someone|somebody|some ?one|seniors?|boys?|guys?|girls?|students?|man|men|teacher|staff|guard|"
               r"class ?fellows?|classmates?) (?:\w+ ){0,2}hit (?:me|us)")

CONFIDENTIAL_PATTERNS = [
    r"harass\w*", r"ragging", r"ragged", r"rag(?:ging)? kar\w*", r"bully\w*", r"bullied",
    r"stalk\w*", r"molest\w*", r"inappropriate(?:ly)? touch\w*", r"touched me", r"sexual\w*",
    r"blackmail\w*", r"threaten\w*", r"threat", r"intimidat\w*", r"abus(?:e|ed|ing|ive)",
    r"tang kar\w*", r"tang kiya", r"chhed\w*", r"ched(?:na|a|te|ti|kani)", r"chherr\w*", r"chhedkhani",
    r"badtameezi", r"dhamki\w*", r"dhamka\w*", r"seniors? (?:ne )?(?:mara|maar|peet)\w*",
    r"discriminat\w*", r"buly\w*", r"bulli\w*", r"bullyed", r"harr?as+\w*", r"haras+\w*",
    # misspellings the fuzzy matcher misses: herassed, harresed, hrassed, harrasmnt
    r"h[ae]r+[ae]s+\w*", r"hr+as+\w*", r"har+[ae]?s+m[ae]?nt\w*",
    r"ragg?ingg+", r"rag+eing", r"rag+ing (?:me|us|him|her|juniors?|freshers?|first ?years?|new students?|students?)",
    r"stalkn?g?", r"stalke?d", r"stalker\w*",
    # following / staring / comments (English)
    r"follow(?:s|ed|ing)? me", r"keeps? following", r"stares? at me", r"staring at me", r"(?:passes|passing|pass) comments?",
    r"comments? on (?:me|my|her|girls?)", r"eve[- ]?teas\w*", r"teas(?:e|es|ed|ing) (?:me|her|us|girls?|juniors?)",
    r"(?:i|we) (?:feel|felt) unsafe", r"not safe around", r"pervert\w*", r"obscene", r"vulgar (?:messages?|comments?|gestures?)",
    r"sends? me (?:messages|pics|pictures|photos|videos)", r"touch(?:es|ed|ing)? me", r"groped?\w*",
    # physical (English)
    r"slap(?:s|ped|ping)? me", r"beat (?:me|us|him|her)(?: up)?", r"beaten up", r"punch(?:ed|es|ing)? me",
    r"push(?:ed|es)? me", r"kick(?:ed|s)? me", r"assault\w*", r"ragged me", _PERSON_HIT,
    # Roman Urdu
    r"pareshan kar\w*", r"tang kart\w*", r"tang kr\w*", r"chher\w*", r"cherr?\w* (?:raha|rahe|rahi|ta|te|ti)\b",
    r"ched(?:na|a|te|ti|kani|raha|rahe|rahi|ta)", r"ched (?:raha|rahe|rahi|ta|te|ti|kar\w*)", r"gaali\w*", r"gali(?:yan| deta| deti| dete| di| de)",
    r"(?:mujhe|mujhay|mujhy|mujy|humein|hamein|hume|use|usay|usko|unhon ne|unho ne|us ne|usne) (?:\w+ ){0,2}(?:maara|mara|maar\w*|peet\w*|thappa?r\w*)",
    r"thappa?r\w*", r"(?:maar|mar) (?:peet|pit)\w*", r"ghoor\w*", r"peecha kar\w*", r"picha kar\w*", r"peeche (?:aata|ata|aate|ate)",
]

_CONF_VOCAB = ["harassed", "harassing", "harassment", "harasser", "harasses", "following", "stalker", "bullying", "bullied", "stalking", "stalked", "molested", "molesting",
               "molestation", "blackmail", "blackmailing", "blackmailed", "intimidating", "intimidation", "intimidated",
               "threatened", "threatening", "discrimination", "discriminating", "ragging", "abusive", "abusing"]
_CONF_RX = re.compile(r"\b(?:" + "|".join(CONFIDENTIAL_PATTERNS) + r")\b")


# Harassment is never "low": floor = high. Physical / sexual / blackmail / threats = critical.
SEVERE_HARASS_PATTERNS = [
    r"molest\w*", r"inappropriate(?:ly)? touch\w*", r"touched me", r"sexual\w*", r"blackmail\w*",
    r"threaten\w*", r"threat", r"dhamki\w*", r"dhamka\w*", r"seniors? (?:ne )?(?:mara|maar|peet)\w*",
    r"assault\w*", r"beat(?:ing|en)?\b", r"hit me", r"rape\w*", r"forced me", r"chhed\w*", r"ched(?:na|a|te|ti|kani)",
    r"chhedkhani",
    r"slap(?:s|ped|ping)? me", _PERSON_HIT, r"beat (?:me|us|him|her)(?: up)?", r"beaten up", r"punch(?:ed|es|ing)? me",
    r"push(?:ed|es)? me", r"kick(?:ed|s)? me", r"touch(?:es|ed|ing)? me", r"groped?\w*", r"pervert\w*", r"obscene",
    r"sends? me (?:pics|pictures|photos|videos)", r"thappa?r\w*", r"(?:maar|mar) (?:peet|pit)\w*",
    r"(?:mujhe|mujhay|mujhy|mujy|humein|hamein|hume|use|usay|usko|unhon ne|unho ne|us ne|usne) (?:\w+ ){0,2}(?:maara|mara|maar\w*|peet\w*|thappa?r\w*)",
]
_SEVERE_RX = re.compile(r"\b(?:" + "|".join(SEVERE_HARASS_PATTERNS) + r")\b")


_SEVERE_VOCAB = ["molested", "molesting", "molestation", "blackmail", "blackmailing", "blackmailed", "threatened",
                 "threatening", "assaulted", "sexually", "sexual"]


def severe_harassment_hit(text: str) -> str | None:
    m = _SEVERE_RX.search(text.lower())
    return m.group(0) if m else _fuzzy_hit(text, _SEVERE_VOCAB)


# ---------------- spam (content-based; rate limiting lives in services) ----------------
_MASH = re.compile(r"asdf|sdfg|dfgh|qwer|wert|zxcv|xcvb|hjkl|jkl;|uiop")


CHATTER = {"hi", "hello", "hey", "hii", "hiii", "yo", "sup", "test", "testing", "tested", "ok", "okay", "lol", "lmao", "haha",
           "hehe", "hmm", "salam", "assalam", "aoa", "asalam", "thanks", "thankyou", "thank", "you", "nice", "good", "wow",
           "abc", "xyz", "demo", "check", "checking", "report", "complaint", "this", "is", "a", "just", "my", "first", "there",
           "bro", "sir", "madam", "kya", "haal", "hai", "how", "are", "u", "ur", "me", "random", "nothing", "bored"}
PROMO_RX = re.compile(r"https?://|www\.|\b\w+\.(?:com|net|pk|xyz|ru)\b|\b(?:lottery|bitcoin|crypto|casino|betting|subscribe|"
                      r"follow me|dm me|click here|whatsapp me|buy now|earn money|promo code|onlyfans|viagra|win (?:cash|prize|iphone))\b|"
                      r"\b(?:\+?92|0)3\d{2}[- ]?\d{7}\b")
ABUSE_RX = re.compile(r"\b(?:fuck\w*|shit\w*|bitch\w*|bastard\w*|idiot\w*|stupid|moron\w*|asshole\w*|dumb ?ass|harami\w*|"
                      r"kamin[ae]\w*|kutt[ae]\w*|chutiy\w*|madarchod|behenchod|bhosd\w*|gandu|loo?du|saala|saale)\b")


def abuse_hit(text: str) -> str | None:
    m = ABUSE_RX.search(text.lower())
    return m.group(0) if m else None


CHATTER |= {"bored", "nothing", "everything", "fine", "all", "good", "great", "app", "website", "sucks", "cool",
             "blah", "bla", "yes", "no", "maybe", "whatever", "idk", "lmfao", "rofl", "xd", "omg", "wtf", "bruh", "yaar",
             "yar", "acha", "theek", "thk", "kuch", "nahi", "bas", "aise", "hi", "hy", "hello", "world", "foo", "bar",
             "asdf", "sample", "dummy", "fake", "joke", "prank", "lorem", "ipsum", "dolor", "sit", "amet", "to", "the",
             "and", "of", "for", "it", "am", "i", "we", "do", "was", "be", "so", "very", "too", "really", "today"}

# Words that say *something is wrong*. A report with none of these, no fault keyword (TOPICS), no safety and no
# harassment signal is not describing a campus problem.
PROBLEM_WORDS = [
    "not", "no", "nahi", "nahin", "nai", "nae", "nhi", "nhe", "na", "mat", "broken", "broke", "break", "breaks", "kharab",
    "khrab", "kharaab", "band", "bund", "tuta", "toota", "tooti", "tuti", "toot", "toota hua", "issue", "issues", "problem",
    "problems", "masla", "masail", "leak", "damage", "damaged", "missing", "lost", "slow", "stuck", "jam", "jammed", "fault",
    "faulty", "error", "down", "dead", "fail", "failed", "failing", "failure", "crack", "cracked", "torn", "stopped", "stop",
    "cant", "can't", "cannot", "won't", "wont", "doesn't", "doesnt", "isn't", "isnt", "aren't", "unable", "overflow",
    "overflowing", "blocked", "clogged", "rats", "rat", "mosquito", "mosquitoes", "pest", "loud", "flicker", "flickering",
    "dangerous", "unsafe", "help", "wrong", "incorrect", "bad", "worst", "terrible", "awful", "poor", "complain", "complaint", "rude", "late", "absent", "chori", "stolen", "theft", "fix",
    "repair", "replace", "wet", "hot", "cold", "garam", "thanda", "nhn",
    "chooha", "chooha", "chuha", "choohay", "chuhay", "chuhe", "makhi", "makhiyan", "machar", "macchar", "kakroch", "chipkali",
    "lizard", "snake", "saanp", "dog", "dogs", "kutta", "billi", "cat", "cats", "monkey", "bees", "termite", "termites",
    "bedbugs", "ants", "rat", "kam nahi", "kaam nahi", "kam nae", "kaam nae",
]
_PROBLEM_RX = re.compile(r"(?<![a-z'])(?:" + "|".join(re.escape(w) for w in sorted(PROBLEM_WORDS, key=len, reverse=True))
                         + r")(?![a-z'])")


_NO_PROBLEM_RX = re.compile(r"\b(?:nothing(?: is)? wrong|no (?:problems?|issues?)|everything(?: is)? (?:fine|ok|okay|good|working)|"
                            r"all (?:good|fine|ok)|koi (?:masla|problem) nahi|sab theek(?: hai)?|just (?:checking|testing))\b")


def describes_problem(text: str) -> bool:
    t = _NO_PROBLEM_RX.sub(" ", text.lower())   # "nothing is wrong, just checking" describes no problem
    return bool(topics(t) or _PROBLEM_RX.search(t) or safety_hit(t) or confidential_hit(t))


_KNOWN_EXTRA = {
    "electrocution", "unprofessional", "responsibilities", "miscommunication", "administration", "infrastructure",
    "maintenance", "department", "laboratory", "engineering", "construction", "temperature", "conditioning"}


_TOPIC_WORDS: set[str] = set()


def _topic_words() -> set[str]:
    if not _TOPIC_WORDS:
        _TOPIC_WORDS.update(w for _, kws in TOPICS.values() for k in kws for w in k.split())
        _TOPIC_WORDS.update(w for p in campus.NAMED.values() for a in p[1] for w in a.split())
    return _TOPIC_WORDS


def gibberish_token(tok: str) -> bool:
    """Random letters, e.g. 'abhdhfirogtugth', 'hjsdf', 'aaaaaaa'. Tuned so Roman Urdu shorthand ('mn', 'nae',
    'rhy', 'kr', 'snr') and English words with consonant clusters ('lights', 'strength') never match."""
    if len(tok) < 5 or tok in CHATTER or tok in _KNOWN_EXTRA or tok in _topic_words():
        return False
    if re.search(r"(.)\1{3,}", tok) or (len(tok) >= 6 and len(set(tok)) <= 2):
        return True                                    # aaaaaa, zzzzzz, hahahahaha
    if not re.search(r"[aeiouy]", tok):
        return True                                    # hjsdf, zmxnc, kjdfg
    if re.search(r"[^aeiouy]{5,}", tok):
        return True                                    # abhdhfiro..., 5 consonants in a row
    if _MASH.search(tok):
        return True
    if len(tok) >= 18:
        return True                                    # no real report word is this long
    vowels = sum(c in "aeiouy" for c in tok)
    return len(tok) >= 9 and vowels / len(tok) < 0.25


def spam_reason(text: str) -> str | None:
    """Junk detection. Safety/harassment text is checked *before* this runs and is never spam.
    Real reports in Roman Urdu with typos ('library mn light nae a rhi') must pass: they name a fault or say
    something is wrong."""
    t = text.lower().strip()
    compact = re.sub(r"\s+", "", t)
    if len(compact) >= 8 and sum(c.isalpha() for c in compact) < 0.4 * len(compact):
        return "mostly symbols/digits"
    tokens = re.findall(r"[a-z]+", t)
    if PROMO_RX.search(t):
        return "advertising / link / phone number"
    if not tokens:
        return "no words"
    if len(tokens) <= 6 and all(w in CHATTER for w in tokens):
        return "greeting or test message, no problem described"
    if len(tokens) >= 3 and len(set(tokens)) == 1:
        return f"one word repeated ('{tokens[0]}')"
    junk = [w for w in tokens if gibberish_token(w)]
    for tok in junk:
        if _MASH.search(tok) or not re.search(r"[aeiouy]", tok) and len(tok) >= 6:
            return f"keyboard mash ('{tok[:12]}')"
    problem = describes_problem(t)
    if junk and (not problem or len(junk) * 2 >= len([w for w in tokens if len(w) >= 3])):
        return f"random letters ('{junk[0][:14]}'), no real problem described"
    if not problem and len(tokens) <= 8:
        return "no campus problem described (say what is broken and where)"
    return None


def confidential_hit(text: str) -> str | None:
    m = _CONF_RX.search(text.lower())
    return m.group(0) if m else _fuzzy_hit(text, _CONF_VOCAB)


# ---------------- prompt-injection detection ----------------

INJECTION_PATTERNS = [
    # Alphabetic patterns carry \b on both sides: "as an ai" must not fire inside "has an air conditioner".
    r"\bignore (?:all |any |the |your |previous |prior |above |earlier )+(?:instructions?|rules?|prompts?|guidelines?)\b",
    r"\bdisregard (?:all |any |the |your |previous |prior |above )*(?:instructions?|rules?|prompts?)\b",
    r"\bforget (?:all |your |previous )+(?:instructions?|rules?)\b",
    r"\b(?:mark|set|make|classify|flag|treat|label|rate) (?:this|it|me|the report|this report|this issue) (?:as |to )?(?:a )?(?:critical|high|urgent|top|safety|confidential|resolved|p0|priority)\b",
    r"\b(?:set|change) (?:the )?(?:priority|status|trust|role|department)\b",
    r"\bpriority\s*[:=]", r"\"priority\"", r"\bis_safety\b", r"\bis_confidential\b", r"\blocation_valid\b",
    r"\bsystem prompt\b", r"\bsystem message\b", r"\bsystem\s*:", r"\bassistant\s*:", r"\byou are now\b",
    r"\bnew instructions\b", r"\bdeveloper mode\b", r"\bjailbreak\b", r"\bas an ai\b", r"\boverride\b",
    r"\bi am (?:the )?(?:admin|administrator|registrar|staff|developer)\b", r"\badmin (?:says|approved|override)\b",
    r"\bgive me (?:admin|trust)\b", r"\bincrease my trust\b", r"\btrust score\b",
    r"</?report", r"```", r"\{\s*\"",
    r"\bpriority (?:critical|high) kar\w*", r"\bcritical mark kar\w*", r"\brules ignore kar\w*",
]
_INJ_RX = re.compile("|".join(INJECTION_PATTERNS))


def injection_hit(text: str) -> str | None:
    m = _INJ_RX.search(text.lower())
    return m.group(0) if m else None


# ---------------- keyword fallback triage ----------------
# topic -> (category, keywords). A *topic* is the specific fault ("fan", "wifi", "projector"); it is what
# separates "Lab 2 fan" from "Lab 2 light" when deciding whether two reports are the same problem.
TOPICS: dict[str, tuple[str, list[str]]] = {
    "internet": ("network", ["wifi", "wi-fi", "wi fi", "wify", "wi-fy", "internet", "network", "lan", "ethernet", "router",
                             "net nahi", "net nai", "net slow", "net", "signal", "broadband", "hotspot"]),
    "portal": ("network", ["lms", "portal", "qalam", "email", "login", "password", "website"]),
    "cooling": ("hvac", ["ac", "a/c", "a.c", "air conditioner", "air conditioning", "aircon", "cooling", "garmi",
                         "thanda nahi", "thandi nahi", "hot", "heater", "heating", "temperature", "geyser"]),
    "fan": ("electrical", ["fan", "fans", "pankha", "pankhe", "exhaust"]),
    "light": ("electrical", ["light", "lights", "bulb", "bulbs", "tubelight", "tube light", "lamp", "roshni", "batti"]),
    "socket": ("electrical", ["socket", "sockets", "switch", "switchboard", "plug", "outlet", "extension board"]),
    "power": ("electrical", ["power", "bijli", "electricity", "ups", "generator", "voltage", "load shedding", "tripped", "breaker"]),
    "wiring": ("electrical", ["wiring", "wire", "wires"]),
    "projector": ("it_equipment", ["projector", "multimedia", "smartboard", "smart board", "beamer"]),
    "computer": ("it_equipment", ["computer", "computers", "pc", "pcs", "monitor", "keyboard", "mouse", "cpu", "laptop",
                                  "system nahi", "desktop"]),
    "printer": ("it_equipment", ["printer", "scanner", "photocopier", "photocopy"]),
    "audio": ("it_equipment", ["speaker", "speakers", "mic", "microphone", "sound system", "audio"]),
    "display": ("it_equipment", ["screen", "display"]),
    "food": ("food", ["food", "khana", "stale", "baasi", "samosa", "samosas", "biryani", "roti", "chai", "tea", "burger",
                      "juice", "menu", "meal", "lunch", "rice", "expired food", "expiry", "expired", "taste"]),
    "food_hygiene": ("food", ["hygiene", "unhygienic", "cockroach", "cockroaches", "keera", "keeray", "insect", "insects",
                              "flies", "worm", "hair in"]),
    "food_price": ("food", ["price", "prices", "overcharg", "overcharge", "overcharging", "mehnga", "mehngi", "expensive"]),
    "library_service": ("library_service", ["book", "books", "kitab", "kitaab", "library card", "issue card", "late fine",
                                            "library fine", "fine lag", "fine charge", "return desk", "catalog", "journal",
                                            "librarian", "borrow", "thesis"]),
    "water": ("plumbing", ["water", "leak", "leaks", "leaking", "tap", "taps", "washroom", "washrooms", "toilet", "toilets",
                           "flush", "paani", "pani", "nalka", "drain", "pipe", "pipes", "water cooler", "dispenser",
                           "sink", "bathroom", "urinal", "seepage", "flood", "flooded", "flooding"]),
    "cleanliness": ("cleanliness", ["dirty", "garbage", "trash", "dustbin", "smell", "smelly", "ganda", "gandi", "gandagi",
                                    "kachra", "dust", "clean", "cleaning", "sweep", "mess", "stink", "stinks"]),
    "furniture": ("furniture", ["chair", "chairs", "desk", "desks", "table", "tables", "bench", "benches", "kursi", "kursiyan",
                                "door", "window", "windows", "whiteboard", "board marker", "marker", "lock"]),
    "safety_equipment": ("safety", ["fire extinguisher", "extinguisher", "fire alarm", "smoke detector", "first aid",
                                    "emergency exit", "exit sign"]),
}

CATEGORY_DEPT = {
    "network": "IS", "hvac": "EE", "electrical": "EE", "food": "Cafeteria",
    "library_service": "Library",
}
# On a tie between categories, the more specific one wins.
CATEGORY_ORDER = ["safety", "network", "hvac", "electrical", "it_equipment", "library_service", "plumbing", "food",
                  "furniture", "cleanliness", "other"]
LOW_HINTS = ["squeak", "squeaky", "noise", "noisy", "paint", "scratch", "wobbly", "dusty", "minor", "slightly",
             "thora", "thoda", "colour", "color", "faded", "awaz"]
HIGH_HINTS = ["whole", "entire", "all computers", "all pcs", "pura", "poora", "sab", "nobody can", "exam",
              "since morning", "since yesterday", "for days", "hafte se", "kal se", "cockroach", "keera",
              "expired", "flood", "flooded", "flooding", "bilkul", "completely", "totally", "at all"]


def _kw_in(text: str, kw: str) -> bool:
    return re.search(r"(?<![a-z])" + re.escape(kw) + r"(?![a-z])", text) is not None


def topics(text: str) -> set[str]:
    """Which specific faults (fan, wifi, projector...) the text mentions."""
    t = text.lower()
    return {name for name, (_, kws) in TOPICS.items() if any(_kw_in(t, k) for k in kws)}


def keyword_category(text: str) -> tuple[str, str | None]:
    t = text.lower()
    score: dict[str, int] = {}
    first: dict[str, str] = {}
    for _, (cat, kws) in TOPICS.items():
        for k in kws:
            if _kw_in(t, k):
                score[cat] = score.get(cat, 0) + 1
                first.setdefault(cat, k)
    if not score:
        return "other", None
    best = max(score, key=lambda c: (score[c], -CATEGORY_ORDER.index(c)))
    return best, first[best]


def department_for(category: str, location: campus.Location | None) -> str:
    if location:  # the place decides: a problem in an IS room goes to IS, in a CSE room to CSE...
        return location.owner
    if category in CATEGORY_DEPT:
        return CATEGORY_DEPT[category]
    if category == "it_equipment":
        return "Labs" if location and location.name.startswith("Lab") else (location.owner if location else "CSE")
    if category == "safety":  # fire/gas/medical with no clearer category: whoever runs the place, else EE
        return location.owner if location else "EE"
    return location.owner if location else "CSE"


def fallback_triage(norm_text: str) -> dict:
    """No LLM: pure keyword triage. Deterministic, always available."""
    loc = campus.match_location(norm_text).location
    cat, kw = keyword_category(norm_text)
    s = safety_hit(norm_text)
    c = confidential_hit(norm_text)
    if c:
        cat = "harassment"
    elif s and cat == "other":
        cat = "safety"
    if s:
        pri = "critical"
    elif c:
        pri = "critical" if severe_harassment_hit(norm_text) else "high"
    elif any(_kw_in(norm_text, h) for h in HIGH_HINTS):
        pri = "high"
    elif cat in ("cleanliness", "furniture", "other") or any(_kw_in(norm_text, h) for h in LOW_HINTS):
        pri = "low"
    else:
        pri = "medium"
    return {
        "department": department_for(cat, loc),
        "location": loc.name if loc else "",
        "category": cat,
        "priority": pri,
        "is_safety": bool(s),
        "is_confidential": bool(c),
        "reason": f"Keyword triage: matched '{s or c or kw or 'nothing specific'}' -> {cat}",
        "location_valid": loc is not None,
    }


PRI_ORDER = ["low", "medium", "high", "critical"]

# ---------------- code guards on top of any triage ----------------

@dataclass
class FinalTriage:
    department: str | None
    location: campus.Location | None
    category: str
    priority: str
    is_safety: bool
    is_confidential: bool
    reason: str
    injection: str | None
    unknown_locations: list[str]
    guards: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "department": self.department, "location": self.location.name if self.location else None,
            "category": self.category, "priority": self.priority, "is_safety": self.is_safety,
            "is_confidential": self.is_confidential, "reason": self.reason,
            "injection": self.injection, "unknown_locations": self.unknown_locations, "guards": self.guards,
        }


def apply_guards(raw_text: str, norm_text: str, proposed: dict, source: str) -> FinalTriage:
    """Everything that matters is decided here, not by the LLM."""
    guards: list[str] = []
    inj = injection_hit(raw_text)
    fb = fallback_triage(norm_text)

    if inj and source == "llm":
        # The text tried to talk to the system. Don't let the LLM's judgement (which
        # may have been steered) set priority/safety/confidentiality: use keyword triage.
        guards.append(f"injection pattern '{inj}' detected -> LLM priority/flags ignored, keyword triage used")
        proposed = {**proposed, "priority": fb["priority"], "is_safety": fb["is_safety"],
                    "is_confidential": fb["is_confidential"]}

    # Location: code decides, from the student's own words only.
    lm = campus.match_location(raw_text)
    loc = lm.location
    llm_loc = (proposed.get("location") or "").strip()
    if loc and llm_loc and llm_loc.lower() != loc.name.lower():
        guards.append(f"LLM location '{llm_loc[:40]}' overridden by whitelist match '{loc.name}'")
    if not loc and llm_loc:
        guards.append(f"LLM location '{llm_loc[:40]}' not found in student's text/whitelist -> ignored")

    # Safety: keyword check OR LLM; keywords can only raise, never lower.
    s_kw = safety_hit(raw_text) or safety_hit(norm_text)
    is_safety = bool(s_kw) or bool(proposed.get("is_safety"))
    if s_kw and not proposed.get("is_safety"):
        guards.append(f"safety keyword '{s_kw}' -> forced is_safety")

    # Confidential: sticky OR.
    c_kw = confidential_hit(raw_text) or confidential_hit(norm_text)
    is_conf = bool(c_kw) or bool(proposed.get("is_confidential"))
    if c_kw and not proposed.get("is_confidential"):
        guards.append(f"confidential keyword '{c_kw}' -> forced confidential")

    pri = proposed.get("priority") if proposed.get("priority") in ("critical", "high", "medium", "low") else fb["priority"]
    if is_safety and pri != "critical":
        guards.append(f"safety -> priority {pri} raised to critical")
        pri = "critical"
    if not is_safety and pri == "critical":
        guards.append("critical without a safety signal -> capped at high")
        pri = "high"

    if is_conf:  # harassment/ragging can never be ranked low by the LLM; severe forms are critical
        sev = severe_harassment_hit(raw_text) or severe_harassment_hit(norm_text)
        floor = "critical" if sev else "high"
        if PRI_ORDER.index(pri) < PRI_ORDER.index(floor):
            guards.append(f"harassment/ragging priority {pri} raised to {floor}" + (f" (severe: '{sev}')" if sev else ""))
            pri = floor

    cat = proposed.get("category") if proposed.get("category") in CATEGORIES else fb["category"]
    if is_conf:
        cat = "harassment"
    elif is_safety and cat == "other":
        cat = "safety"

    dept = proposed.get("department")
    if dept not in DEPARTMENTS:
        guards.append(f"department '{str(dept)[:20]}' invalid -> rule-based routing")
        dept = department_for(cat, loc)
    if not is_conf:
        # Place ownership beats the LLM and the fault type: a problem in a department's room
        # goes to that department only.
        if loc and dept != loc.owner:
            guards.append(f"department '{dept}' conflicts with location '{loc.name}' -> routed to owner {loc.owner}")
            dept = loc.owner
    if is_conf:
        dept = None  # never routed to any department queue
        guards.append("confidential -> removed from all department queues (admin only)")

    reason = re.sub(r"\s+", " ", str(proposed.get("reason") or fb["reason"]))[:160]
    return FinalTriage(dept, loc, cat, pri, is_safety, is_conf, reason, inj, lm.unknown_mentions, guards)
