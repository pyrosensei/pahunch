"""Pronounce server-rendered delivery dates without changing their stored values."""

import re

_EN = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"]
_HI = ["जनवरी", "फ़रवरी", "मार्च", "अप्रैल", "मई", "जून", "जुलाई", "अगस्त", "सितंबर", "अक्टूबर", "नवंबर", "दिसंबर"]
_NUM_HI = ["शून्य", "एक", "दो", "तीन", "चार", "पाँच", "छह", "सात", "आठ", "नौ", "दस", "ग्यारह", "बारह", "तेरह", "चौदह", "पंद्रह", "सोलह", "सत्रह", "अठारह", "उन्नीस", "बीस", "इक्कीस", "बाईस", "तेईस", "चौबीस", "पच्चीस", "छब्बीस", "सत्ताईस", "अट्ठाईस", "उनतीस", "तीस", "इकतीस", "बत्तीस", "तैंतीस", "चौंतीस", "पैंतीस", "छत्तीस", "सैंतीस", "अड़तीस", "उनतालीस", "चालीस", "इकतालीस", "बयालीस", "तैंतालीस", "चवालीस", "पैंतालीस", "छियालीस", "सैंतालीस", "अड़तालीस", "उनचास", "पचास", "इक्यावन", "बावन", "तिरपन", "चौवन", "पचपन", "छप्पन", "सत्तावन", "अट्ठावन", "उनसठ"]
_SMALL = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen"]
_TENS = {20: "twenty", 30: "thirty", 40: "forty", 50: "fifty"}
_MONTHS = {name.casefold(): i + 1 for i, name in enumerate(_EN)}
_MONTHS.update({name[:3].casefold(): i + 1 for i, name in enumerate(_EN)})
_MONTHS.update({name: i + 1 for i, name in enumerate(_HI)})
_MONTHS.update({"फरवरी": 2, "सितम्बर": 9, "नवम्बर": 11, "दिसम्बर": 12})
_MONTH_RE = "|".join(sorted(map(re.escape, _MONTHS), key=len, reverse=True))
_DATE = re.compile(rf"(?<!\d)([0-3]?\d)\s+({_MONTH_RE})(?!\w)", re.IGNORECASE)
_TIME = re.compile(r"(?<![\d:])([01]?\d|2[0-3]):([0-5]\d)(?![\d:])")


def _english_number(value: int) -> str:
    if value < 20:
        return _SMALL[value]
    tens, units = divmod(value, 10)
    return _TENS[tens * 10] + (f" {_SMALL[units]}" if units else "")


def _time(hour: int, minute: int, language: str) -> str:
    twelve = hour % 12 or 12
    if language == "hi-IN":
        period = "रात" if hour < 5 or hour >= 20 else "सुबह" if hour < 12 else "दोपहर" if hour < 17 else "शाम"
        clock = f"{_NUM_HI[twelve]} बजे" if minute == 0 else f"{_NUM_HI[twelve]} बजकर {_NUM_HI[minute]} मिनट"
        return f"{period} {clock}"
    period = "at night" if hour < 5 or hour >= 21 else "in the morning" if hour < 12 else "in the afternoon" if hour < 17 else "in the evening"
    clock = _english_number(twelve)
    clock += " o'clock" if minute == 0 else (" oh " if minute < 10 else " ") + _english_number(minute)
    return f"{clock} {period}"


def spoken_text(text: str, language: str = "hi-en") -> str:
    """Convert date/time labels only; keep identifiers and customer names intact.

    This runs at the synthesis boundary. Transcripts and structured plan/window
    values retain the exact original text and timestamps.
    """
    def date(match: re.Match) -> str:
        day = int(match[1])
        if not 1 <= day <= 31:
            return match[0]
        month = _MONTHS[match[2].casefold()]
        if language == "hi-IN":
            return f"{_NUM_HI[day]} {_HI[month - 1]}"
        return f"{_english_number(day)} {_EN[month - 1]}"

    rendered = _DATE.sub(date, text)
    # A range dash belongs to the clock label, not an order number or name.
    rendered = re.sub(r"(?<=\d{2}:\d{2})\s*[-–—]\s*(?=\d{1,2}:\d{2})", " से " if language == "hi-IN" else " to ", rendered)
    rendered = _TIME.sub(lambda m: _time(int(m[1]), int(m[2]), language), rendered)
    return re.sub(r"\bIST\b", "भारतीय समय" if language == "hi-IN" else "Indian Standard Time", rendered)
