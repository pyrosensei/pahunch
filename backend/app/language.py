import re
import unicodedata

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


def response_language(text: str) -> str:
    if _HINDI.search(text):
        return "hi-IN"
    if set(re.findall(r"[a-z]+", text.casefold())) & _HINGLISH_WORDS:
        return "hi-en"
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
        "unclear": "Sorry, mujhe yeh theek se samajh nahi aaya. Kya aap time window aur handoff ek baar phir bata sakte hain?",
        "unavailable": "Sorry, main abhi plan check nahi kar payi. Kya aap thodi der baad phir bata sakte hain?",
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
        "ack": "Ji, main check kar rahi hoon.",
    },
    "en-IN": {
        "unclear": "Sorry, I did not understand that clearly. Please tell me your preferred delivery window and handoff.",
        "unavailable": "Sorry, I could not check the plan right now. Please try again later.",
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
        "ack": "I am checking that for you.",
    },
    "hi-IN": {
        "unclear": "क्षमा कीजिए, आपकी बात स्पष्ट नहीं समझ पाई। कृपया डिलीवरी का समय और पार्सल किसे देना है, फिर बताइए।",
        "unavailable": "क्षमा कीजिए, अभी योजना जाँच नहीं पाई। कृपया थोड़ी देर बाद फिर बताइए।",
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
        "ack": "जी, मैं इसकी जाँच कर रही हूँ।",
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


def wording_error(message: str, language: str, *, proposal: bool = False, respond: bool = False):
    compact = " ".join(unicodedata.normalize("NFKC", message).split())
    if not compact:
        return "EMPTY_MESSAGE"
    if len(compact) > 280:
        return "MESSAGE_TOO_LONG"
    if OTP_WORD.search(compact) or _UNSAFE.search(compact):
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
        if "{" in compact or "}" in compact:
            return "INVALID_PLACEHOLDERS"
        if not language_matches(compact, language):
            return "LANGUAGE_MISMATCH"
        if respond and _RESPONSE_ACTION.search(compact):
            return "UNSAFE_WORDING"
        if len([p for p in re.split(r"[.!?।]+", compact) if p.strip()]) > 2:
            return "MESSAGE_TOO_LONG"
    return None


def conversational_input(text: str) -> str | None:
    cleaned = " ".join(re.sub(r"[?!.।,]", " ", text.casefold()).split())
    if cleaned in {"hello", "helo", "hi", "hey", "namaste", "नमस्ते", "हेलो"}:
        return "greeting"
    if cleaned in {
        "can you hear me",
        "is my voice audible",
        "kya meri aawaz aa rahi hai",
        "kya aapko meri aawaz aa rhi hai",
        "kya aapko meri aawaz aa rahi hai",
        "क्या आपको मेरी आवाज सुनाई दे रही है",
        "क्या मेरी आवाज आ रही है",
    }:
        return "heard"
    return None


def is_rejection(text: str) -> bool:
    return bool(re.match(r"^\s*(?:no\b|nope\b|nahi\b|nahin\b|नहीं|नही)", text, re.IGNORECASE))
