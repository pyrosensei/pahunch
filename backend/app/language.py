import re
import unicodedata
from dataclasses import dataclass, field

_HINDI = re.compile(r"[\u0900-\u097f]")
_HINGLISH_WORDS = {
    "haan",
    "han",
    "ha",
    "namaste",
    "theek",
    "thik",
    "hai",
    "hain",
    "mujhe",
    "mera",
    "meri",
    "mere",
    "aap",
    "aapko",
    "aapki",
    "tum",
    "kya",
    "nahi",
    "nahin",
    "kal",
    "aaj",
    "shaam",
    "subah",
    "baje",
    "dena",
    "de",
    "kardo",
    "chahiye",
    "rahungi",
    "raha",
    "rahi",
    "ghar",
    "padosi",
    "ji",
    "ko",
    "se",
    "ka",
    "ki",
    "ke",
    "boliye",
    "bata",
    "samajh",
    "gayi",
    "karna",
    "kar",
    "karo",
    "kaun",
    "kaise",
    "kyun",
    "aawaz",
    "awaz",
    "sunai",
    "bataiye",
    "nhi",
    "rhi",
}
OTP_WORD = re.compile(
    r"\botp\b|ओ\s*टी\s*पी|one[ -]time (?:password|code)|verification code",
    re.IGNORECASE,
)
_NUMBER_WORD = re.compile(
    r"\b(one|two|three|four|five|six|seven|eight|nine|zero|ek|do|teen|char|chaar|"
    r"paanch|panch|chhe|che|saat|aath|nau|shunya)\b|एक|दो|तीन|चार|पाँच|पांच|छह|सात|आठ|नौ|शून्य",
    re.IGNORECASE,
)
_UNSAFE = re.compile(
    r"\botp\b|ओ\s*टी\s*पी|\d|\b(cancel\w*|refund\w*|compensation|done|completed|committed|"
    r"successfully|approved|authorized|permitted|allowed|guarantee\w*)\b|[<>]|"
    r"\b(?:was|is|has been)\s+(?:delivered|confirmed|booked|rescheduled|finalized)\b|"
    r"(?:confirm|book|reschedule|update|deliver)\w*\s+(?:ho|kar)\s+(?:gaya|gayi|diya|di)|"
    r"\b(?:will|shall|can|may)\s+(?:leave|deliver|reschedule|handoff|approve|commit)\b|"
    r"\b(?:i am|i'm)\s+(?:a\s+)?(?:real\s+)?(?:human|person)\b|"
    r"(?:दे|कर|हो)\s*(?:दिया|दी|गया|गई)|अनुमति|मंजूर|गारंटी|रिफंड|रद्द",
    re.IGNORECASE,
)
_RESPONSE_ACTION = re.compile(
    r"\b(leave|security|neighbou?r|handoff|hand-off|reschedule|retry|confirm|finali[sz]e|"
    r"prepaid|cod|payment|cash|scheduled|window|tomorrow|today|padosi|chhod\w*|handover)\b|"
    r"सिक्योरिटी|पड़ोसी|सौंप|छोड़|पुनर्निर्धार|कन्फर्म|पुष्टि|मंजूर|भुगतान|प्रीपेड|नकद|निर्धारित",
    re.IGNORECASE,
)
_DELIVERY_COMMITMENT = re.compile(
    r"\b(?:will|shall|would|going to|i'll|i will)\b[^.?!]{0,25}?\b(?:left|handed\s+over|given|dropped|placed|dispatched)\b|"
    r"\b(?:hand\s+over|hand|give|drop|place|leave)\w*\b[^.?!]{0,20}?\b(?:parcel|package|"
    r"to\s+(?:the\s+|my\s+)?(?:guard|security|neighbou?r|neighbor|door|watchman|porter))\b|"
    r"\b(?:hand|give|leave|drop)\w*\s+(?:it|them|the\s+parcel|parcel)\s+to\s+\w+|"
    r"(?:सौंप|छोड़|दे|रख|भेज)\s*(?:दूँगा|दूंगा|देंगे|जाएगा|जायेगा|दिया जाएगा|दिया जायेगा)|"
    r"(?:पड़ोसी|सिक्योरिटी|गार्ड|नौकर|दरवाज़े)[^.।?!]{0,20}(?:सौंप|छोड़|दे|रख|भेज)\w*\s*"
    r"(?:दूँगा|दूंगा|देंगे|जाएगा|जायेगा|दिया जाएगा|दिया जायेगा|सकते हैं|सकती हैं)",
    re.IGNORECASE,
)
_HANDOFF_PERMISSION = re.compile(
    r"\b(?:parcel|package|delivery)\b[^.?!।]{0,45}\b(?:de|bhej|deliver|handover)\s+sakt[aei]\b|"
    r"(?:पार्सल|डिलीवरी)[^.?!।]{0,45}(?:दे|भेज|सौंप)\s*सकत[ाीे]",
    re.IGNORECASE,
)


def response_language(text: str) -> str:
    if _HINDI.search(text):
        return "hi-IN"
    if set(re.findall(r"[a-z]+", text.casefold())) & _HINGLISH_WORDS:
        return "hi-en"
    return "en-IN"


_TIME_CUE = re.compile(
    r"\b(?:am|pm|baje|subah|dopahar|shaam|raat|morning|afternoon|evening|night|o'clock)\b|"
    r"बजे|सुबह|दोपहर|शाम|रात",
    re.IGNORECASE,
)
_DIGIT_WORDS = {
    "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
    "six": "6", "seven": "7", "eight": "8", "nine": "9",
    "ek": "1", "do": "2", "teen": "3", "char": "4", "chaar": "4", "paanch": "5",
    "panch": "5", "chhe": "6", "che": "6", "saat": "7", "aath": "8", "nau": "9",
    "shunya": "0",
    "शून्य": "0", "एक": "1", "दो": "2", "तीन": "3", "चार": "4", "पाँच": "5",
    "पांच": "5", "छह": "6", "सात": "7", "आठ": "8", "नौ": "9",
}
_HOUSE_CUE = re.compile(r"house|हाउस|number|नंबर|नम्बर|\bno\b|ghar|मकान", re.IGNORECASE)


def conversation_language(text: str, previous: str = "hi-IN") -> str:
    """Keep acknowledgements and brief Hindi replies in the established language."""
    if re.fullmatch(r"\s*(?:please\s+)?(?:speak|reply|continue|talk)(?: to me)?(?: in)? english[.!?\s]*", text, re.IGNORECASE):
        return "en-IN"
    if re.fullmatch(r"\s*(?:please\s+)?(?:hindi mein (?:bolo|boliye|baat karo)|हिंदी में (?:बोलिए|बोलो|बात करें))[.!?।\s]*", text, re.IGNORECASE):
        return "hi-IN"
    normalized = " ".join(text.casefold().split()).strip(".!?। ")
    acknowledgements = {
        "yes", "yeah", "okay", "ok", "haan", "han", "ha", "ji", "theek hai",
        "हाँ", "हां", "हा", "जी", "ठीक", "ठीक है", "नमस्ते", "namaste",
    }
    if normalized in acknowledgements:
        return previous
    words = re.findall(r"[a-z]+", text.casefold())
    hindi_words = re.findall(r"[\u0900-\u097f]+", text)
    if hindi_words and not words and len(hindi_words) <= 2:
        return previous
    # A Hinglish call stays Hinglish when the customer answers in Devanagari.
    # Full Hindi is only for an explicit request, handled above.
    if hindi_words and previous == "hi-en":
        return "hi-en"
    if hindi_words:
        # One borrowed word must not change a whole conversation. Substantial
        # evidence in both scripts represents mixed speech, not pure Hindi.
        if len(hindi_words) == 1 and len(words) >= 3:
            return "en-IN"
        if len(words) >= 3 and len(words) >= len(hindi_words) / 2:
            return "hi-en"
        return "hi-IN"
    cues = sum(w in _HINGLISH_WORDS for w in words)
    if cues >= 2 and cues / max(len(words), 1) >= 0.15:
        return "hi-en"
    if cues and len(words) <= 3:
        return previous
    return "en-IN"


def language_matches(text: str, language: str) -> bool:
    if language == "hi-IN":
        return bool(_HINDI.search(text))
    if _HINDI.search(text):
        return False
    detected = response_language(text)
    return detected == language


def has_otp_disclosure(text: str) -> bool:
    longest = current = 0
    for token in re.findall(r"[A-Za-z\u0900-\u097f]+", text):
        current = current + 1 if _NUMBER_WORD.fullmatch(token) else 0
        longest = max(longest, current)
    return bool(
        (
            OTP_WORD.search(text)
            and (
                re.search(r"(?<!\d)(?:\d[ -]?){4,8}(?!\d)", text)
                or longest >= 4
                or "[redacted]" in text
            )
        )
        or re.fullmatch(r"\s*(?:\d[ -]?){4,8}\s*", text)
    )


def redact_customer_text(text: str) -> str:
    if not has_otp_disclosure(text):
        return text
    language = response_language(text)
    return {"en-IN": "OTP [redacted]", "hi-IN": "ओटीपी [redacted]", "hi-en": "Mera OTP [redacted]"}[
        language
    ]


def recorded_turn_text(text: str, speaker: str, payload: dict) -> str:
    """Keep server-rendered order facts readable; never exempt customer/model text."""
    meta = payload.get("planner", {})
    if (speaker == "AGENT" and isinstance(meta, dict)
            and meta.get("model") == "rules:delivery-opening"
            and (payload.get("opening") is True or meta.get("fallback_reason") == "OUTBOUND_DELIVERY_PURPOSE")):
        # These two producers interpolate only stored order/catalog facts, never
        # a customer OTP. Generic model responses and all customer turns still
        # pass through the conservative OTP-disclosure redactor below.
        return text
    return redact_customer_text(text)


def redact_payload(value):
    if isinstance(value, str):
        return redact_customer_text(value)
    if isinstance(value, dict):
        return {key: redact_payload(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_payload(item) for item in value]
    return value


MESSAGES = {
    "hi-en": {
        "unclear": "Aap scheduled time par parcel lenge, ya delivery ka doosra time chahiye?",
        "unavailable": "Mujhe abhi jawab taiyar karne mein technical dikkat aa rahi hai. Kripya thodi der baad dobara koshish kijiye.",
        "rate_limit": "Jawab taiyar karne wali service ki usage limit abhi poori ho gayi hai. Kripya thodi der baad dobara koshish kijiye.",
        "confirmation_required": "Confirm karne ke liye haan ya theek hai boliye. Phir main isi plan ko aage badha sakti hoon.",
        "confirmation_accepted": "Theek hai, main is plan ko final karne ke liye aage badha rahi hoon.",
        "confirmation_question": "Kya main is plan ko aage badhaun?",
        "escalation": "Main is request ko human team ko bhej rahi hoon. Woh aapse details confirm karenge.",
        "otp_refusal": "Main OTP kabhi share nahi karti. OTP aapke phone par aata hai; delivery ke time courier ko dijiye.",
        "identity": "Main SwiftCart ki automated delivery assistant hoon, insaan nahi. Aap tracking link se apna order verify kar sakte hain.",
        "greeting": "Namaste, main SwiftCart ki automated delivery assistant hoon. Boliye, delivery ke liye kya chahiye?",
        "heard": "Haan, aapki aawaz aa rahi hai. Boliye, delivery ke liye kya chahiye?",
        "repeat_plan": "Aapne yeh plan mana kiya hai. Koi doosra time ya handoff bataiye.",
        "turn_limit": "Hum is call mein plan tay nahi kar paye. Yahin call khatam karte hain; abhi koi plan final nahi hua hai.",
        "ack": "Ji, aapki baat sun li.",
    },
    "en-IN": {
        "unclear": "Will you receive the parcel at the scheduled time, or would you prefer another delivery time?",
        "unavailable": "I'm having a technical problem preparing my reply. Please try again in a little while.",
        "rate_limit": "The service that prepares my replies has reached its usage limit. Please try again later.",
        "confirmation_required": "Please explicitly say yes to this plan before I take it forward.",
        "confirmation_accepted": "Thank you, I will submit this plan for the final check.",
        "confirmation_question": "May I take this plan forward?",
        "escalation": "I am referring this request to the human team for review.",
        "otp_refusal": "I never collect or share your OTP. Give it only to the courier at delivery.",
        "identity": "I am SwiftCart's automated delivery assistant. You can verify your order using its tracking link.",
        "greeting": "Hello, I am SwiftCart's automated delivery assistant. How can I help with your delivery?",
        "heard": "Yes, I can hear you. What would you like to arrange for your delivery?",
        "repeat_plan": "You rejected that plan. Please suggest a different delivery window or handoff.",
        "turn_limit": "We could not agree on a plan within this call. I will end the conversation here; no plan has been finalized.",
        "ack": "I heard you.",
    },
    "hi-IN": {
        "unclear": "आप तय समय पर पार्सल लेंगे, या डिलीवरी का दूसरा समय चाहिए?",
        "unavailable": "मुझे अभी जवाब तैयार करने में तकनीकी दिक्कत आ रही है। कृपया थोड़ी देर बाद दोबारा कोशिश कीजिए।",
        "rate_limit": "जवाब तैयार करने वाली सेवा की उपयोग सीमा अभी पूरी हो गई है। कृपया थोड़ी देर बाद दोबारा कोशिश कीजिए।",
        "confirmation_required": "इस योजना को आगे बढ़ाने से पहले कृपया स्पष्ट रूप से हाँ कहिए।",
        "confirmation_accepted": "धन्यवाद, अब इस योजना को अंतिम जाँच के लिए आगे बढ़ा रही हूँ।",
        "confirmation_question": "क्या मैं इस योजना को आगे बढ़ाऊँ?",
        "escalation": "इस अनुरोध को समीक्षा के लिए मानव सहायता टीम को भेज रही हूँ।",
        "otp_refusal": "मैं आपका ओटीपी कभी नहीं माँगती या साझा करती। इसे डिलीवरी के समय केवल कूरियर को दीजिए।",
        "identity": "मैं SwiftCart की स्वचालित डिलीवरी सहायक हूँ। आप ट्रैकिंग लिंक से अपना ऑर्डर जाँच सकते हैं।",
        "greeting": "नमस्ते, मैं SwiftCart की स्वचालित डिलीवरी सहायक हूँ। बताइए, आपकी क्या सहायता कर सकती हूँ?",
        "heard": "हाँ, आपकी आवाज सुनाई दे रही है। डिलीवरी के लिए क्या व्यवस्था चाहिए?",
        "repeat_plan": "आपने यह योजना अस्वीकार की है। कृपया कोई दूसरा समय या पार्सल सौंपने का तरीका बताइए।",
        "turn_limit": "इस बातचीत में योजना तय नहीं हो पाई। अब बातचीत समाप्त करती हूँ; कोई योजना अंतिम नहीं हुई है।",
        "ack": "जी, आपकी बात सुन ली।",
    },
}


def phrase(key: str, language: str = "hi-en") -> str:
    return MESSAGES[language][key]


def translate_fixed(message: str, language: str) -> str:
    for key, original in MESSAGES["hi-en"].items():
        if message == original:
            return phrase(key, language)
    return message


WRAPPER_PREFIXES = {
    "en-IN": ("", "Understood.", "Thanks for clarifying.", "For the revised instruction:"),
    "hi-IN": ("", "समझ गई।", "स्पष्ट करने के लिए धन्यवाद।", "बदले हुए निर्देश के लिए:"),
    "hi-en": ("", "Samajh gayi.", "Aapki baat samajh gayi.", "Badle hue instruction ke liye:"),
}
PLAN_PLACEHOLDERS = "{plan_summary} {confirmation_question}"
FACT_TEMPLATES = {
    "en-IN": (
        "Your scheduled delivery window is {order_window}.",
        "The payment mode is {payment_mode}.",
        "{handoff_policy}",
    ),
    "hi-IN": (
        "आपकी डिलीवरी का निर्धारित समय {order_window} है।",
        "भुगतान का तरीका {payment_mode} है।",
        "{handoff_policy}",
    ),
    "hi-en": (
        "Aapki delivery ka scheduled time {order_window} hai.",
        "Is order ka payment mode {payment_mode} hai.",
        "{handoff_policy}",
    ),
}


_NUMERIC_EXPRESSION = re.compile(r"\d{1,2}:\d{2}(?::\d{2})?|\d+")
_MONTH_NAMES = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
    "जनवरी": 1, "फ़रवरी": 2, "फरवरी": 2, "मार्च": 3, "अप्रैल": 4, "मई": 5,
    "जून": 6, "जुलाई": 7, "अगस्त": 8, "सितंबर": 9, "अक्टूबर": 10, "नवंबर": 11,
    "दिसंबर": 12,
}
_MONTH_ANY = re.compile(
    r"(?<!\w)(?:"
    + "|".join(re.escape(name) for name in sorted(_MONTH_NAMES, key=len, reverse=True))
    + r")(?!\w)",
    re.IGNORECASE,
)
_DAY_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
    "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
    "twenty": 20, "twenty-one": 21, "twenty-two": 22, "twenty-three": 23,
    "twenty-four": 24, "twenty-five": 25, "twenty-six": 26,
    "twenty-seven": 27, "twenty-eight": 28, "twenty-nine": 29,
    "thirty": 30, "thirty-one": 31,
    "ek": 1, "do": 2, "teen": 3, "char": 4, "chaar": 4, "paanch": 5,
    "panch": 5, "chhe": 6, "che": 6, "saat": 7, "aath": 8, "nau": 9,
    "das": 10, "gyarah": 11, "barah": 12, "terah": 13, "chaudah": 14,
    "pandrah": 15, "solah": 16, "satrah": 17, "atharah": 18, "unnis": 19,
    "bees": 20, "ikkees": 21, "bais": 22, "teis": 23, "chaubis": 24,
    "pachchis": 25, "chhabbis": 26, "sattais": 27, "atthais": 28,
    "untees": 29, "tees": 30, "ikattis": 31,
    "एक": 1, "दो": 2, "तीन": 3, "चार": 4, "पाँच": 5, "पांच": 5,
    "छह": 6, "सात": 7, "आठ": 8, "नौ": 9, "दस": 10, "ग्यारह": 11,
    "बारह": 12, "तेरह": 13, "चौदह": 14, "पंद्रह": 15, "सोलह": 16,
    "सत्रह": 17, "अठारह": 18, "उन्नीस": 19, "बीस": 20, "इक्कीस": 21,
    "बाईस": 22, "तेईस": 23, "चौबीस": 24, "पच्चीस": 25, "छब्बीस": 26,
    "सत्ताईस": 27, "अट्ठाईस": 28, "उनतीस": 29, "तीस": 30, "इकतीस": 31,
}
_CLOCK_HOURS = {word: value for word, value in _DAY_WORDS.items() if value <= 12}
_CLOCK_HOURS.update({"zero": 0, "shunya": 0, "शून्य": 0})


@dataclass
class KnownFacts:
    """Facts the backend already knows, so a reply may repeat them unchanged."""

    order_ids: set[str] = field(default_factory=set)
    dates: set[tuple[int, int]] = field(default_factory=set)
    times: set[str] = field(default_factory=set)
    numbers: set[str] = field(default_factory=set)
    quotes: set[str] = field(default_factory=set)


def normalize_text(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def _day_before(prefix: str) -> int | None:
    match = re.search(r"(\d{1,2})\s+$", prefix)
    if match:
        return int(match.group(1))
    match = re.search(r"([a-z\u0900-\u097f-]+)\s+$", prefix)
    if match:
        return _DAY_WORDS.get(match.group(1))
    return None


def _day_after(suffix: str) -> int | None:
    match = re.match(r"\s+(\d{1,2})\b", suffix)
    if match:
        return int(match.group(1))
    match = re.match(r"\s+([a-z\u0900-\u097f-]+)\b", suffix)
    if match:
        return _DAY_WORDS.get(match.group(1))
    return None


def stated_dates(text: str) -> set[tuple[int, int]]:
    """Dates a speaker asked for, in any script and spelled-out or numeric form."""
    normalized = normalize_text(text)
    found: set[tuple[int, int]] = set()
    for match in _MONTH_ANY.finditer(normalized):
        month = _MONTH_NAMES.get(match.group(0))
        if month is None:
            continue
        day = _day_before(normalized[: match.start()]) or _day_after(
            normalized[match.end() :]
        )
        if day is not None and 1 <= day <= 31:
            found.add((day, month))
    return found


def _mentioned_date_match(text: str) -> tuple[int, int, str] | None:
    normalized = normalize_text(text)
    for match in _MONTH_ANY.finditer(normalized):
        month = _MONTH_NAMES.get(normalize_text(match.group(0)))
        if month is None:
            continue
        day = _day_before(normalized[: match.start()]) or _day_after(
            normalized[match.end() :]
        )
        if day is None or not 1 <= day <= 31:
            continue
        return day, month, match.group(0)
    return None


def mentioned_date_parts(text: str) -> tuple[int, int] | None:
    """Return the day and month number the customer named."""
    found = _mentioned_date_match(text)
    if found is None:
        return None
    return found[0], found[1]


def mentioned_date(text: str, language: str) -> str | None:
    """Return the date the customer named, for a date-aware clarification."""
    found = _mentioned_date_match(text)
    if found is None:
        return None
    return f"{found[0]} {found[2]}"


def _period_name(token: str) -> str | None:
    if token == "am":
        return "ampm-am"
    if token == "pm":
        return "ampm-pm"
    if re.fullmatch(r"सुबह|subah|morning", token):
        return "morning"
    if re.fullmatch(r"दोपहर|dopahar|afternoon|noon", token):
        return "afternoon"
    if re.fullmatch(r"शाम|shaam|evening", token):
        return "evening"
    if re.fullmatch(r"रात|raat|night", token):
        return "night"
    return None


def _clock_token(token: str) -> tuple[int, int] | None:
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", token)
    if match:
        hour, minute = int(match.group(1)), int(match.group(2))
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return hour, minute
        return None
    attached = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?(am|pm)", token)
    if attached:
        hour = int(attached.group(1))
        minute = int(attached.group(2) or 0)
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return hour, minute
    if token.isdigit():
        hour = int(token)
        return (hour, 0) if 0 <= hour <= 23 else None
    hour = _CLOCK_HOURS.get(token)
    if hour is None:
        return None
    return hour, 0


def _resolve_clock(period: str, hour: int, minute: int) -> tuple[int, int] | None:
    if not 0 <= minute <= 59:
        return None
    if period == "ampm-am":
        if hour == 12:
            hour = 0
        elif hour > 12:
            return None
    elif period == "ampm-pm":
        if hour < 12:
            hour += 12
        elif hour > 23:
            return None
    elif period == "morning":
        if not 1 <= hour <= 12:
            return None
    elif period == "afternoon":
        if hour == 12:
            pass
        elif 1 <= hour <= 6:
            hour += 12
        elif not 13 <= hour <= 18:
            return None
    elif period == "evening":
        if 1 <= hour <= 11:
            hour += 12
        elif not 12 <= hour <= 23:
            return None
    elif period == "night":
        if hour == 12:
            hour = 0
        elif 1 <= hour <= 5:
            pass
        elif 6 <= hour <= 11:
            hour += 12
        elif not 0 <= hour <= 23:
            return None
    else:
        return None
    return hour, minute


def mentioned_times(text: str) -> list[tuple[int, int]]:
    """Every explicit clock in the utterance, in order, without duplicates."""
    tokens = re.sub(r"[,.!?।]", " ", normalize_text(text)).split()
    clocks = [(index, _clock_token(token)) for index, token in enumerate(tokens)]
    clocks = [(index, clock) for index, clock in clocks if clock is not None]
    periods = [(index, _period_name(token)) for index, token in enumerate(tokens)]
    periods = [(index, period) for index, period in periods if period is not None]
    found: list[tuple[int, int]] = []
    for index, (hour, minute) in clocks:
        attached = re.fullmatch(r"\d{1,2}(?::\d{2})?(am|pm)", tokens[index])
        if attached:
            resolved = _resolve_clock(f"ampm-{attached.group(1)}", hour, minute)
            if resolved is not None:
                found.append(resolved)
            continue
        nearest: tuple[int, str] | None = None
        for period_index, period in periods:
            distance = abs(period_index - index)
            limit = 1 if period.startswith("ampm") else 4
            if distance == 0 or distance > limit:
                continue
            if any(min(index, period_index) < other < max(index, period_index) for other, _ in clocks):
                continue
            if nearest is None or distance < nearest[0] or (
                distance == nearest[0] and period_index > index
            ):
                nearest = (distance, period)
        if nearest is None:
            if ":" in tokens[index] and (hour >= 13 or hour == 0):
                found.append((hour, minute))
            continue
        resolved = _resolve_clock(nearest[1], hour, minute)
        if resolved is not None:
            found.append(resolved)
    return list(dict.fromkeys(found))


def mentioned_time(text: str) -> tuple[int, int] | None:
    """Return one explicit clock time, or None when the utterance is ambiguous."""
    found = mentioned_times(text)
    if len(found) != 1:
        return None
    return found[0]


def mixed_script(text: str) -> bool:
    """A reply that switches between Devanagari and Latin in one sentence."""
    return bool(_HINDI.search(text) and re.search(r"[A-Za-z]", text))


def number_words(text: str) -> set[str]:
    """Digit strings for number words, so a spoken hour can be repeated."""
    found: set[str] = set()
    for token in re.findall(r"[A-Za-z\u0900-\u097f]+|\d+", normalize_text(text)):
        if token.isdigit():
            found.add(str(int(token)))
            if int(token) < 10:
                found.add(f"{int(token):02d}")
            continue
        digit = _DIGIT_WORDS.get(token)
        if digit is None:
            continue
        found.add(digit)
        if len(digit) == 1:
            found.add(f"0{digit}")
    return found


def recipient_label(field: str, quote: str) -> str:
    """Keep a courier label to the name or house number inside a longer sentence."""
    text = " ".join(quote.split())
    if field == "named_recipient":
        match = re.match(r"(.+?)\s+(?:नाम|naam|name)\b", text, re.IGNORECASE)
        if match and 1 <= len(match.group(1).split()) <= 3:
            return match.group(1).strip(" ,.")
        return text
    if field != "recipient_address" or _HOUSE_CUE.search(text) is None:
        return text
    digits = []
    for token in normalize_text(text).split():
        if token.isdigit():
            digits.append(token)
        elif token in _DIGIT_WORDS:
            digits.append(_DIGIT_WORDS[token])
    return "".join(digits) if digits else text


def _apply_known_dates(text: str, facts: KnownFacts) -> tuple[str, bool]:
    """Accept only dates the backend or the customer already stated."""
    masked = list(text)
    known = True
    for match in _MONTH_ANY.finditer(text):
        month = _MONTH_NAMES.get(normalize_text(match.group(0)))
        if month is None:
            continue
        before = text[: match.start()]
        after = text[match.end() :]
        day_before = re.search(r"(\d{1,2})\s+$", before)
        day_after = re.match(r"\s+(\d{1,2})\b", after)
        if day_before is None and day_after is None and not _is_date_context(before, after):
            continue
        day = int((day_before or day_after).group(1)) if (day_before or day_after) else None
        if day is not None and (day, month) in facts.dates:
            start = match.start() - len(day_before.group(0)) if day_before else match.start()
            for index in range(start, match.end()):
                masked[index] = " "
            continue
        if _said_by_customer(match.group(0), facts):
            for index in range(match.start(), match.end()):
                masked[index] = " "
            continue
        known = False
    return "".join(masked), known


def _is_date_context(before: str, after: str) -> bool:
    prefix = re.search(r"(?:\b(?:in|on|at|of|for|by|until|till|through|during|since|from)|[मेंकोतकसे])\s*$", before, re.IGNORECASE)
    suffix = re.match(r"\s*(?:\b(?:in|on|at|of|for|by|until|till|through|during|since|from)|[मेंकोतकसे])\b", after, re.IGNORECASE)
    return bool(prefix or suffix)


def _said_by_customer(phrase: str, facts: KnownFacts) -> bool:
    wording = normalize_text(phrase)
    return bool(wording) and any(wording in quote for quote in facts.quotes)


def _numbers_are_known(compact: str, facts: KnownFacts) -> bool:
    """Allow only numbers the backend already knows, so invented facts stay blocked."""
    masked, dates_ok = _apply_known_dates(compact, facts)
    if not dates_ok:
        return False
    for value in _NUMERIC_EXPRESSION.findall(masked):
        if ":" in value:
            if value not in facts.times:
                return False
        elif value not in facts.numbers and value not in facts.order_ids:
            return False
    return True


def wording_error(message: str, language: str, *, proposal: bool = False, respond: bool = False, source_text: str = "", known_facts: KnownFacts | None = None, clarification: bool = False, relax_language: bool = False, wording_text: str | None = None):
    compact = " ".join(unicodedata.normalize("NFKC", message).split())
    if not compact:
        return "EMPTY_MESSAGE"
    if len(compact) > 280:
        return "MESSAGE_TOO_LONG"
    # Server-filled facts are trusted; only the model's own words face the guards.
    guarded = compact if wording_text is None else " ".join(
        unicodedata.normalize("NFKC", wording_text).split()
    )
    checked = guarded
    if clarification:
        checked = re.sub(r"\{(?:order_window|recipient|recipient_address)\}", "", checked)
        if _HANDOFF_PERMISSION.search(checked):
            return "UNVALIDATED_HANDOFF_PERMISSION"
    if not proposal:
        facts = KnownFacts(
            order_ids=set(known_facts.order_ids) if known_facts else set(),
            dates=set(known_facts.dates) if known_facts else set(),
            times=set(known_facts.times) if known_facts else set(),
            numbers=(set(known_facts.numbers) if known_facts else set())
            | (
                number_words(source_text)
                if _TIME_CUE.search(source_text)
                else set(re.findall(r"\d+", source_text))
            ),
            quotes=set(known_facts.quotes) if known_facts else set(),
        )
        if source_text.strip():
            facts.quotes.add(normalize_text(source_text))
        # A date the customer asked for is a fact, even when the reply spells it
        # in another script ("बारह नवंबर" -> "12 November").
        for quote in facts.quotes:
            facts.dates.update(stated_dates(quote))
        if not _apply_known_dates(guarded, facts)[1]:
            return "UNSAFE_WORDING"
        if (facts.numbers or facts.order_ids or facts.dates or facts.times) and _numbers_are_known(guarded, facts):
            checked = re.sub(r"\d+", "", checked)
    if OTP_WORD.search(compact) or _UNSAFE.search(checked):
        return "UNSAFE_WORDING"
    if proposal:
        if compact.count("{plan_summary}") != 1 or compact.count("{confirmation_question}") != 1:
            return "INVALID_PLACEHOLDERS"
        remaining = compact.replace("{plan_summary}", "").replace("{confirmation_question}", "")
        if "{" in remaining or "}" in remaining:
            return "INVALID_PLACEHOLDERS"
        if remaining.strip() and not language_matches(remaining, language):
            return "LANGUAGE_MISMATCH"
        if compact not in [f"{p} {PLAN_PLACEHOLDERS}".strip() for p in WRAPPER_PREFIXES[language]]:
            return "UNRECOGNIZED_WRAPPER"
    else:
        if "{" in checked or "}" in checked:
            return "INVALID_PLACEHOLDERS"
        if not relax_language and not language_matches(compact, language):
            return "LANGUAGE_MISMATCH"
        if respond and _RESPONSE_ACTION.search(checked):
            return "UNSAFE_WORDING"
        if _DELIVERY_COMMITMENT.search(checked):
            return "UNSAFE_WORDING"
        # The server's own availability reply is three sentences, so a model
        # repeating it verbatim must not be rejected for length.
        if len([p for p in re.split(r"[.!?।]+", compact) if p.strip()]) > 3:
            return "MESSAGE_TOO_LONG"
    return None


def conversational_input(text: str) -> str | None:
    cleaned = " ".join(
        unicodedata.normalize("NFKC", re.sub(r"[?!.।,]", " ", text.casefold())).split()
    )
    if cleaned in {"hello", "helo", "hi", "hey", "namaste", "नमस्ते", "हेलो"}:
        return "greeting"
    audio_check = cleaned
    for greeting in ("hello ", "hi ", "हेलो ", "नमस्ते "):
        if audio_check.startswith(greeting):
            audio_check = audio_check[len(greeting):]
            break
    if audio_check in {
        "can you hear me",
        "is my voice audible",
        "kya meri aawaz aa rahi hai",
        "kya aapko meri aawaz aa rhi hai",
        "kya aapko meri aawaz aa rahi hai",
        "क्या आपको मेरी आवाज सुनाई दे रही है",
        "क्या मेरी आवाज आ रही है",
    }:
        return "heard"
    if len(audio_check.split()) <= 8 and (
        re.search(r"\b(?:hear|hearing|audible)\b", audio_check)
        or re.search(r"आवाज़?|a{1,2}w+a{1,2}z|सुनाई|sunai", audio_check)
    ):
        speaker_cue = re.search(
            r"\b(?:me|you|my|voice)\b|मेरी|मेरा|मुझे|आपको|आपकी|aapko|meri|mera|mujhe",
            audio_check,
        )
        heard_cue = re.search(
            r"\b(?:can you hear|do you hear|are you hearing)\b|आ रही|आ रहा|aa rahi|aa raha|aa rhi",
            audio_check,
        )
        if speaker_cue and heard_cue:
            return "heard"
    return None


def is_rejection(text: str) -> bool:
    return bool(re.match(r"^\s*(?:no\b|nope\b|nahi\b|nahin\b|नहीं|नही)", text, re.IGNORECASE))


def is_call_purpose_question(text: str) -> bool:
    """Small conversational routing rule, not a delivery-instruction parser."""
    cleaned = " ".join(re.sub(r"[?!.।,]", " ", text.casefold()).split())
    return cleaned in {
        "why are you calling", "why did you call", "what is this call about",
        "what do you want to talk about", "what is this about", "what do you need",
        "kya baat karni hai", "kya baat karni hai aapko", "kyun phone kiya",
        "kisliye call kiya", "aapne kyon phone kiya", "kya baat hai",
        "क्या बात करनी है आपको", "क्या बात करनी है", "आपने फोन क्यों किया",
        "आपने फ़ोन क्यों किया", "क्यों फोन किया", "कॉल क्यों किया", "क्या काम है",
        "हाँ क्या बात करनी है आपको", "हां क्या बात करनी है आपको",
        "ji boliye", "haan boliye", "haan bataiye", "yes go on",
        "हाँ बताइए", "हां बताइए", "जी बताइए", "जी बोलिए",
    }


def is_availability_acceptance(text: str) -> bool:
    cleaned = " ".join(re.sub(r"[?!.।,]", " ", text.casefold()).split())
    return cleaned in {
        "yes", "yes i am available", "yes i'll be home", "yes i will be home",
        "haan", "han", "haan main ghar par hoon", "haan available hoon",
        "हाँ", "हां", "जी हाँ", "हाँ मैं घर पर हूँ", "हां मैं घर पर हूं",
        "हाँ उपलब्ध हूँ", "हां उपलब्ध हूं",
    }
