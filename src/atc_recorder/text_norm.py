"""Bidirectional text normalization for ATC training data.

Converts between formatted ATC text (e.g. "AAL123 RWY 19") and
spoken form (e.g. "american one two three runway one niner").
The spoken form is required for acoustic model training since ASR
models cannot train on digits or abbreviations.
"""

import re
from typing import Optional

from .entities import AIRLINE_CALLSIGN_MAP, ICAO_TO_AIRLINE
from .logging import get_logger

logger = get_logger(__name__)

_DIGIT_TO_SPOKEN: dict[str, str] = {
    "0": "zero",
    "1": "one",
    "2": "two",
    "3": "three",
    "4": "four",
    "5": "five",
    "6": "six",
    "7": "seven",
    "8": "eight",
    "9": "niner",
}

_LETTER_TO_PHONETIC: dict[str, str] = {
    "A": "alpha",
    "B": "bravo",
    "C": "charlie",
    "D": "delta",
    "E": "echo",
    "F": "foxtrot",
    "G": "golf",
    "H": "hotel",
    "I": "india",
    "J": "juliet",
    "K": "kilo",
    "L": "lima",
    "M": "mike",
    "N": "november",
    "O": "oscar",
    "P": "papa",
    "Q": "quebec",
    "R": "romeo",
    "S": "sierra",
    "T": "tango",
    "U": "uniform",
    "V": "victor",
    "W": "whiskey",
    "X": "xray",
    "Y": "yankee",
    "Z": "zulu",
}

DEFAULT_WAYPOINT_PRONUNCIATIONS: dict[str, str] = {
    "FRDMM": "freedom",
    "CAPSS": "capps",
    "NUMMY": "nummy",
    "IRONS": "irons",
    "CLIPR": "clipper",
    "TRUPS": "troops",
    "DEALE": "deal",
    "SKILS": "skills",
    "SOOKI": "sookie",
    "SCRAM": "scram",
    "DOCTR": "doctor",
    "CLTCH": "clutch",
    "REBLL": "rebel",
    "JDUBB": "j dub",
    "HORTO": "horto",
    "WYNGS": "wings",
    "ENSUE": "ensue",
    "OJAAY": "o j",
    "FLUKY": "fluky",
    "KRANT": "krant",
    "LURAY": "luray",
}

# Patterns for formatted ATC tokens
_ICAO_CALLSIGN_RE = re.compile(r"\b([A-Z]{3})(\d{1,5}[A-Z]?)\b")
_RUNWAY_RE = re.compile(r"\b(?:RWY|RUNWAY)\s*(\d{1,2})\s*([LRC])?\b", re.IGNORECASE)
_FLIGHT_LEVEL_RE = re.compile(r"\bFL\s*(\d{2,3})\b", re.IGNORECASE)
_ALTITUDE_FT_RE = re.compile(r"\b(\d{1,5})\s*(?:FT|FEET)\b", re.IGNORECASE)
_HEADING_RE = re.compile(r"\bHDG\s*(\d{3})\b", re.IGNORECASE)
_FREQUENCY_RE = re.compile(r"\b(\d{3})\.(\d{1,3})\b")
_NNUMBER_RE = re.compile(r"\b(N)(\d{1,5})([A-Z]{0,2})\b")
_SID_STAR_RE = re.compile(r"\b([A-Z]{3,5})(\d{1,2})\b")
_PURE_DIGITS_RE = re.compile(r"\b(\d{2,5})\b")

_RUNWAY_SUFFIX_SPOKEN = {"L": "left", "R": "right", "C": "center"}


def _digits_to_spoken(digits: str) -> str:
    """Convert digit string to spoken form: '123' -> 'one two three'."""
    return " ".join(_DIGIT_TO_SPOKEN.get(d, d) for d in digits)


def _letters_to_phonetic(letters: str) -> str:
    """Convert letter string to NATO phonetic: 'AB' -> 'alpha bravo'."""
    return " ".join(_LETTER_TO_PHONETIC.get(c.upper(), c) for c in letters)


class TextNormalizer:
    """Converts formatted ATC text to spoken form for acoustic model training."""

    def __init__(self, waypoint_map: Optional[dict[str, str]] = None):
        self.waypoints = {
            **(waypoint_map or {}),
            **DEFAULT_WAYPOINT_PRONUNCIATIONS,
        }
        if waypoint_map:
            self.waypoints.update(waypoint_map)

    def to_spoken(self, text: str) -> str:
        """Convert formatted ATC text to fully spoken form.

        Conservative: only normalizes tokens it recognizes, passes
        through natural speech unchanged.
        """
        result = text

        # ICAO callsigns: AAL123 -> american one two three
        def _replace_icao(m: re.Match) -> str:
            icao = m.group(1)
            flight = m.group(2)
            airline = ICAO_TO_AIRLINE.get(icao, "")
            if airline:
                digits = "".join(c for c in flight if c.isdigit())
                suffix = "".join(c for c in flight if c.isalpha())
                spoken = f"{airline} {_digits_to_spoken(digits)}"
                if suffix:
                    spoken += f" {_letters_to_phonetic(suffix)}"
                return spoken
            return m.group(0)

        result = _ICAO_CALLSIGN_RE.sub(_replace_icao, result)

        # N-numbers: N123AB -> november one two three alpha bravo
        def _replace_nnumber(m: re.Match) -> str:
            digits = m.group(2)
            suffix = m.group(3)
            spoken = f"november {_digits_to_spoken(digits)}"
            if suffix:
                spoken += f" {_letters_to_phonetic(suffix)}"
            return spoken

        result = _NNUMBER_RE.sub(_replace_nnumber, result)

        # Runways: RWY 19L -> runway one niner left
        def _replace_runway(m: re.Match) -> str:
            digits = m.group(1)
            suffix = (m.group(2) or "").upper()
            spoken = f"runway {_digits_to_spoken(digits)}"
            if suffix in _RUNWAY_SUFFIX_SPOKEN:
                spoken += f" {_RUNWAY_SUFFIX_SPOKEN[suffix]}"
            return spoken

        result = _RUNWAY_RE.sub(_replace_runway, result)

        # Flight levels: FL350 -> flight level three five zero
        def _replace_fl(m: re.Match) -> str:
            return f"flight level {_digits_to_spoken(m.group(1))}"

        result = _FLIGHT_LEVEL_RE.sub(_replace_fl, result)

        # Altitudes with units: 3000FT -> three thousand feet
        def _replace_altitude(m: re.Match) -> str:
            return f"{_digits_to_spoken(m.group(1))} feet"

        result = _ALTITUDE_FT_RE.sub(_replace_altitude, result)

        # Headings: HDG270 -> heading two seven zero
        def _replace_heading(m: re.Match) -> str:
            return f"heading {_digits_to_spoken(m.group(1))}"

        result = _HEADING_RE.sub(_replace_heading, result)

        # Frequencies: 124.700 -> one two four point seven zero zero
        def _replace_freq(m: re.Match) -> str:
            whole = m.group(1)
            dec = m.group(2)
            return f"{_digits_to_spoken(whole)} point {_digits_to_spoken(dec)}"

        result = _FREQUENCY_RE.sub(_replace_freq, result)

        # SID/STAR with number: FRDMM6 -> freedom six
        def _replace_sid_star(m: re.Match) -> str:
            name = m.group(1).upper()
            num = m.group(2)
            if name in self.waypoints:
                return f"{self.waypoints[name]} {_digits_to_spoken(num)}"
            return m.group(0)

        result = _SID_STAR_RE.sub(_replace_sid_star, result)

        # Standalone waypoints without numbers
        for code, pronunciation in self.waypoints.items():
            result = re.sub(rf"\b{re.escape(code)}\b", pronunciation, result, flags=re.IGNORECASE)

        # Remaining standalone digit sequences
        def _replace_digits(m: re.Match) -> str:
            return _digits_to_spoken(m.group(1))

        result = _PURE_DIGITS_RE.sub(_replace_digits, result)

        # Clean up whitespace
        result = " ".join(result.lower().split())
        return result
