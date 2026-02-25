"""Entity extraction from ATC transcript text using regex-based NER."""

import re
from dataclasses import dataclass

# Spoken airline name -> ICAO 3-letter code
AIRLINE_CALLSIGN_MAP: dict[str, str] = {
    "air canada": "ACA",
    "air france": "AFR",
    "air wisconsin": "AWI",
    "alaska": "ASA",
    "allegiant": "AAY",
    "american": "AAL",
    "atlas": "GTI",
    "breeze": "MXY",
    "british": "BAW",
    "cactus": "AWE",  # Spirit Airlines
    "cair": "CRQ",  # Air Creebec (rare, but in FAA list)
    "cape air": "KAP",
    "cathay": "CPA",
    "citrus": "JBU",  # JetBlue (alternate callsign)
    "comair": "COM",
    "compass": "CPZ",
    "continental": "COA",
    "delta": "DAL",
    "dynasty": "CAL",  # China Airlines
    "eastern": "EAL",
    "emirates": "UAE",
    "endeavor": "EDV",
    "envoy": "ENY",
    "etihad": "ETD",
    "express jet": "ASQ",
    "fedex": "FDX",
    "frontier": "FFT",
    "go jet": "GJS",
    "hawaiian": "HAL",
    "horizon": "QXE",
    "jazz": "JZA",
    "jetblue": "JBU",
    "korean": "KAL",
    "lufthansa": "DLH",
    "mesa": "ASH",
    "midwest": "MEP",
    "northwest": "NWA",
    "piedmont": "PDT",
    "pinnacle": "FLG",
    "psa": "JIA",
    "qantas": "QFA",
    "republic": "RPA",
    "shuttle america": "TCF",
    "singapore": "SIA",
    "skywest": "SKW",
    "southwest": "SWA",
    "spirit": "NKS",
    "sun country": "SCX",
    "swiss": "SWR",
    "turkish": "THY",
    "united": "UAL",
    "ups": "UPS",
    "virgin": "VIR",
    "volaris": "VOI",
    "westjet": "WJA",
}

# ICAO 3-letter codes for reverse lookups
ICAO_TO_AIRLINE: dict[str, str] = {v: k for k, v in AIRLINE_CALLSIGN_MAP.items()}

# NATO phonetic alphabet -> letter
_PHONETIC_MAP: dict[str, str] = {
    "alpha": "A",
    "alfa": "A",
    "bravo": "B",
    "charlie": "C",
    "delta": "D",
    "echo": "E",
    "foxtrot": "F",
    "golf": "G",
    "hotel": "H",
    "india": "I",
    "juliet": "J",
    "juliett": "J",
    "kilo": "K",
    "lima": "L",
    "mike": "M",
    "november": "N",
    "oscar": "O",
    "papa": "P",
    "quebec": "Q",
    "romeo": "R",
    "sierra": "S",
    "tango": "T",
    "uniform": "U",
    "victor": "V",
    "whiskey": "W",
    "xray": "X",
    "x-ray": "X",
    "yankee": "Y",
    "zulu": "Z",
}

# Spoken number words -> digit
_SPOKEN_DIGIT_MAP: dict[str, str] = {
    "zero": "0",
    "oh": "0",
    "one": "1",
    "wun": "1",
    "two": "2",
    "three": "3",
    "tree": "3",
    "four": "4",
    "fower": "4",
    "five": "5",
    "fife": "5",
    "six": "6",
    "seven": "7",
    "eight": "8",
    "ait": "8",
    "nine": "9",
    "niner": "9",
}

# Common runway designators
_RUNWAY_SUFFIX_MAP: dict[str, str] = {
    "left": "L",
    "right": "R",
    "center": "C",
}


@dataclass
class EntityMention:
    """A single entity extracted from transcript text."""

    entity_type: str
    raw_text: str
    normalized: str
    confidence: float
    start_offset: int
    end_offset: int


def _spoken_to_digits(text: str) -> str:
    """Convert spoken number words to digit string. E.g. 'one two three' -> '123'."""
    result = []
    for word in text.lower().split():
        if word in _SPOKEN_DIGIT_MAP:
            result.append(_SPOKEN_DIGIT_MAP[word])
        elif word.isdigit():
            result.append(word)
        elif len(word) > 1 and all(c.isdigit() for c in word):
            result.extend(word)
    return "".join(result)


def _spoken_to_letters(text: str) -> str:
    """Convert phonetic alphabet words to letters. E.g. 'alpha bravo' -> 'AB'."""
    result = []
    for word in text.lower().split():
        if word in _PHONETIC_MAP:
            result.append(_PHONETIC_MAP[word])
        elif len(word) == 1 and word.isalpha():
            result.append(word.upper())
    return "".join(result)


# --- Callsign patterns ---

# Airline callsign: "Delta 1234" or "Delta one two three four"
_AIRLINE_NAMES_PATTERN = "|".join(
    re.escape(name) for name in sorted(AIRLINE_CALLSIGN_MAP.keys(), key=len, reverse=True)
)
_AIRLINE_SPOKEN_RE = re.compile(
    rf"\b({_AIRLINE_NAMES_PATTERN})\s+"
    r"((?:(?:zero|oh|one|wun|two|three|tree|four|fower|five|fife|six|seven|eight|ait|niner|nine|\d)\s*){1,5})"
    r"(?:\s+(?:heavy|super))?\b",
    re.IGNORECASE,
)

# Airline callsign with numeric digits: "Delta 1234"
_AIRLINE_NUMERIC_RE = re.compile(
    rf"\b({_AIRLINE_NAMES_PATTERN})\s+(\d{{1,5}}[A-Za-z]?)" r"(?:\s+(?:heavy|super))?\b",
    re.IGNORECASE,
)

# ICAO 3-letter + number: "DAL1234" or "DAL 1234"
_ICAO_RE = re.compile(r"\b([A-Z]{3})\s?(\d{1,5}[A-Z]?)\b")

# GA/N-number: "N123AB" or "November 1 2 3 Alpha Bravo"
_NNUMBER_RE = re.compile(r"\b(N\d{1,5}[A-Z]{0,2})\b", re.IGNORECASE)

_GA_PHONETIC_RE = re.compile(
    r"\b[Nn]ovember\s+"
    r"((?:(?:zero|oh|one|wun|two|three|tree|four|fower|five|fife|six|seven|eight|ait|niner|nine|\d)\s*){1,5})"
    r"\s*"
    r"((?:(?:alpha|alfa|bravo|charlie|delta|echo|foxtrot|golf|hotel|india|juliet|juliett|"
    r"kilo|lima|mike|oscar|papa|quebec|romeo|sierra|tango|uniform|victor|whiskey|xray|x-ray|yankee|zulu)\s*){0,2})"
    r"\b",
    re.IGNORECASE,
)

# --- Runway pattern ---
_RUNWAY_RE = re.compile(
    r"\brunway\s+"
    r"((?:(?:zero|oh|one|wun|two|three|tree|four|fower|five|fife|six|seven|eight|ait|niner|nine|\d)\s*){1,3})"
    r"(?:\s*(left|right|center|[LRC]))?\b",
    re.IGNORECASE,
)

_RUNWAY_NUMERIC_RE = re.compile(
    r"\brunway\s+(\d{1,2})\s*(left|right|center|[LRC])?\b",
    re.IGNORECASE,
)

# --- Altitude pattern ---
_ALTITUDE_FL_RE = re.compile(
    r"\bflight\s+level\s+"
    r"((?:(?:zero|oh|one|wun|two|three|tree|four|fower|five|fife|six|seven|eight|ait|niner|nine|\d)\s*){1,4})"
    r"\b",
    re.IGNORECASE,
)

_ALTITUDE_THOUSAND_RE = re.compile(
    r"\b(\d{1,3})\s*thousand(?:\s+(\d{1,3})\s*hundred)?\b",
    re.IGNORECASE,
)

# --- Frequency pattern ---
_FREQ_RE = re.compile(
    r"\b(?:contact\s+\w+\s+(?:on\s+)?)?(\d{2,3})\s*(?:point|decimal)\s*(\d{1,3})\b",
    re.IGNORECASE,
)

_CONTACT_FREQ_RE = re.compile(
    r"\bcontact\s+([\w\s]+?)\s+(?:on\s+)?(\d{2,3})\s*(?:point|decimal)\s*(\d{1,3})\b",
    re.IGNORECASE,
)


def _extract_airline_callsigns(text: str) -> list[EntityMention]:
    """Extract airline callsigns like 'Delta 1234' or 'Delta one two three four'."""
    mentions: list[EntityMention] = []

    for m in _AIRLINE_NUMERIC_RE.finditer(text):
        airline_name = m.group(1).lower()
        flight_num = m.group(2).upper()
        icao = AIRLINE_CALLSIGN_MAP.get(airline_name, airline_name[:3].upper())
        normalized = f"{icao}{flight_num}"
        mentions.append(
            EntityMention(
                entity_type="callsign",
                raw_text=m.group(0).strip(),
                normalized=normalized,
                confidence=0.90,
                start_offset=m.start(),
                end_offset=m.end(),
            )
        )

    for m in _AIRLINE_SPOKEN_RE.finditer(text):
        already_covered = any(em.start_offset <= m.start() < em.end_offset for em in mentions)
        if already_covered:
            continue
        airline_name = m.group(1).lower()
        digits = _spoken_to_digits(m.group(2))
        if not digits:
            continue
        icao = AIRLINE_CALLSIGN_MAP.get(airline_name, airline_name[:3].upper())
        normalized = f"{icao}{digits}"
        mentions.append(
            EntityMention(
                entity_type="callsign",
                raw_text=m.group(0).strip(),
                normalized=normalized,
                confidence=0.75,
                start_offset=m.start(),
                end_offset=m.end(),
            )
        )

    return mentions


def _extract_icao_callsigns(text: str) -> list[EntityMention]:
    """Extract ICAO-format callsigns like 'DAL1234'."""
    mentions: list[EntityMention] = []
    for m in _ICAO_RE.finditer(text):
        code = m.group(1)
        if code not in ICAO_TO_AIRLINE:
            continue
        num = m.group(2)
        normalized = f"{code}{num}"
        mentions.append(
            EntityMention(
                entity_type="callsign",
                raw_text=m.group(0),
                normalized=normalized,
                confidence=0.95,
                start_offset=m.start(),
                end_offset=m.end(),
            )
        )
    return mentions


def _extract_ga_callsigns(text: str) -> list[EntityMention]:
    """Extract general aviation callsigns (N-numbers)."""
    mentions: list[EntityMention] = []

    for m in _NNUMBER_RE.finditer(text):
        raw = m.group(1).upper()
        mentions.append(
            EntityMention(
                entity_type="callsign",
                raw_text=m.group(0),
                normalized=raw,
                confidence=0.85,
                start_offset=m.start(),
                end_offset=m.end(),
            )
        )

    for m in _GA_PHONETIC_RE.finditer(text):
        already = any(em.start_offset <= m.start() < em.end_offset for em in mentions)
        if already:
            continue
        digits = _spoken_to_digits(m.group(1))
        letters = _spoken_to_letters(m.group(2)) if m.group(2) else ""
        if not digits:
            continue
        normalized = f"N{digits}{letters}"
        mentions.append(
            EntityMention(
                entity_type="callsign",
                raw_text=m.group(0).strip(),
                normalized=normalized,
                confidence=0.70,
                start_offset=m.start(),
                end_offset=m.end(),
            )
        )

    return mentions


def _extract_runways(text: str) -> list[EntityMention]:
    """Extract runway references like 'runway 19 left' or 'runway 1 niner left'."""
    mentions: list[EntityMention] = []

    for m in _RUNWAY_NUMERIC_RE.finditer(text):
        num = m.group(1)
        suffix_raw = (m.group(2) or "").strip().lower()
        suffix = _RUNWAY_SUFFIX_MAP.get(suffix_raw, suffix_raw.upper())
        normalized = f"RWY{num}{suffix}"
        mentions.append(
            EntityMention(
                entity_type="runway",
                raw_text=m.group(0).strip(),
                normalized=normalized,
                confidence=0.90,
                start_offset=m.start(),
                end_offset=m.end(),
            )
        )

    for m in _RUNWAY_RE.finditer(text):
        already = any(em.start_offset <= m.start() < em.end_offset for em in mentions)
        if already:
            continue
        digits = _spoken_to_digits(m.group(1))
        if not digits:
            continue
        suffix_raw = (m.group(2) or "").strip().lower()
        suffix = _RUNWAY_SUFFIX_MAP.get(suffix_raw, suffix_raw.upper())
        normalized = f"RWY{digits}{suffix}"
        mentions.append(
            EntityMention(
                entity_type="runway",
                raw_text=m.group(0).strip(),
                normalized=normalized,
                confidence=0.80,
                start_offset=m.start(),
                end_offset=m.end(),
            )
        )

    return mentions


def _extract_altitudes(text: str) -> list[EntityMention]:
    """Extract altitude references like 'flight level 350' or 'five thousand'."""
    mentions: list[EntityMention] = []

    for m in _ALTITUDE_FL_RE.finditer(text):
        digits = _spoken_to_digits(m.group(1))
        if not digits:
            continue
        normalized = f"FL{digits}"
        mentions.append(
            EntityMention(
                entity_type="altitude",
                raw_text=m.group(0).strip(),
                normalized=normalized,
                confidence=0.85,
                start_offset=m.start(),
                end_offset=m.end(),
            )
        )

    for m in _ALTITUDE_THOUSAND_RE.finditer(text):
        thousands = int(m.group(1))
        hundreds = int(m.group(2)) if m.group(2) else 0
        alt = thousands * 1000 + hundreds * 100
        normalized = f"{alt}FT"
        mentions.append(
            EntityMention(
                entity_type="altitude",
                raw_text=m.group(0).strip(),
                normalized=normalized,
                confidence=0.80,
                start_offset=m.start(),
                end_offset=m.end(),
            )
        )

    return mentions


def _extract_frequencies(text: str) -> list[EntityMention]:
    """Extract ATC frequency references like 'contact approach 124.7' or '118 point 95'."""
    mentions: list[EntityMention] = []

    for m in _CONTACT_FREQ_RE.finditer(text):
        whole = m.group(2)
        decimal = m.group(3)
        normalized = f"{whole}.{decimal}"
        mentions.append(
            EntityMention(
                entity_type="frequency",
                raw_text=m.group(0).strip(),
                normalized=normalized,
                confidence=0.85,
                start_offset=m.start(),
                end_offset=m.end(),
            )
        )

    for m in _FREQ_RE.finditer(text):
        already = any(em.start_offset <= m.start() < em.end_offset for em in mentions)
        if already:
            continue
        whole = m.group(1)
        decimal = m.group(2)
        freq_val = float(f"{whole}.{decimal}")
        if not (118.0 <= freq_val <= 137.0):
            continue
        normalized = f"{whole}.{decimal}"
        mentions.append(
            EntityMention(
                entity_type="frequency",
                raw_text=m.group(0).strip(),
                normalized=normalized,
                confidence=0.75,
                start_offset=m.start(),
                end_offset=m.end(),
            )
        )

    return mentions


def extract_entities(text: str, min_confidence: float = 0.0) -> list[EntityMention]:
    """Extract all entities from ATC transcript text.

    Returns entities sorted by start_offset.
    """
    if not text or not text.strip():
        return []

    mentions: list[EntityMention] = []
    mentions.extend(_extract_airline_callsigns(text))
    mentions.extend(_extract_icao_callsigns(text))
    mentions.extend(_extract_ga_callsigns(text))
    mentions.extend(_extract_runways(text))
    mentions.extend(_extract_altitudes(text))
    mentions.extend(_extract_frequencies(text))

    if min_confidence > 0:
        mentions = [m for m in mentions if m.confidence >= min_confidence]

    # Deduplicate overlapping mentions, preferring higher confidence
    mentions.sort(key=lambda m: (m.start_offset, -m.confidence))
    deduped: list[EntityMention] = []
    for m in mentions:
        overlaps = any(
            d.start_offset <= m.start_offset < d.end_offset
            or d.start_offset < m.end_offset <= d.end_offset
            for d in deduped
        )
        if not overlaps:
            deduped.append(m)

    deduped.sort(key=lambda m: m.start_offset)
    return deduped


def extract_callsigns(text: str, min_confidence: float = 0.0) -> list[EntityMention]:
    """Extract only callsign entities from text."""
    return [e for e in extract_entities(text, min_confidence) if e.entity_type == "callsign"]
