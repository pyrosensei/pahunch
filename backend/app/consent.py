"""Shared explicit-agreement validation for planner and customer API turns."""

import re

_AFFIRMATIVE_WORDS = {
    "yahi",
    "haan",
    "han",
    "ha",
    "yes",
    "yeah",
    "yep",
    "sure",
    "agree",
    "agreed",
    "pushti",
    "confirm",
    "confirmed",
    "accept", "approved", "absolutely", "fine", "perfect",
    "theek",
    "thik",
    "sahi",
    "ok",
    "okay",
    "bilkul",
    "zaroor",
    "chalo",
}
_CONFIRMATION_WORDS = _AFFIRMATIVE_WORDS | {
    "i", "this", "that", "the", "it", "with", "proceed",
    "date", "window", "time", "arrangement", "delivery", "instructions", "proposed", "revised",
    "main", "mein", "mai", "isi", "ki", "karta", "karti", "hoon", "hun",
    "dijiye",
    "aage",
    "aap",
    "aapka",
    "aapke",
    "ahead",
    "badh",
    "badhaiye",
    "badhao",
    "badho",
    "bhi",
    "do",
    "go",
    "hai",
    "ho",
    "is",
    "iss",
    "jaaye",
    "jaye",
    "ji",
    "kar",
    "kardo",
    "de",
    "dena",
    "hi",
    "use",
    "us",
    "usko",
    "kariye",
    "karo",
    "ke",
    "kijiye",
    "ko",
    "par",
    "plan",
    "please",
    "saath",
    "to",
    "toh",
}

# Prisma STT returns Devanagari script; map the confirmation vocabulary to the
# Latin words the gate already knows. Anything unmapped fails the whitelist.
_DEVANAGARI_WORD_MAP = {
    "मैं": "main", "में": "mein", "की": "ki",
    "पुष्टि": "pushti", "करता": "karta", "करती": "karti",
    "सहमत": "agree", "करदीजिए": "kardo",
    "हूँ": "hoon", "हूं": "hoon",
    "इस": "is",
    "इसी": "is",
    "इसे": "is",
    "को": "ko",
    "योजना": "plan",
    "यही": "yahi",
    "दीजिए": "dijiye",
    "हाँ": "haan",
    "हां": "haan",
    "हा": "ha",
    "ठीक": "theek",
    "ठिक": "thik",
    "सही": "sahi",
    "बिल्कुल": "bilkul",
    "बिलकुल": "bilkul",
    "ज़रूर": "zaroor",
    "जरूर": "zaroor",
    "चलो": "chalo",
    "ओके": "ok",
    "ओक": "ok",
    "कन्फर्म": "confirm",
    "है": "hai",
    "हैं": "hai",
    # Prisma sometimes renders Hindi "hai" with the Marathi form; seen live.
    "आहे": "hai",
    "कर": "kar",
    "करो": "karo",
    "करिए": "kariye",
    "करिये": "kariye",
    "कीजिए": "kijiye",
    "कीजिये": "kijiye",
    "दे": "de",
    "देना": "dena",
    "दो": "do",
    "आगे": "aage",
    "बढ़ाओ": "badhao",
    "बढाओ": "badhao",
    "बढ़ाइए": "badhaiye",
    "बढाइए": "badhaiye",
    "उसे": "use",
    "उस": "us",
    "उसको": "usko",
    "ही": "hi",
    "दें": "de",
    "प्लान": "plan",
    "प्लीज़": "please",
    "प्लीज": "please",
    "जी": "ji",
}

def is_explicit_confirmation(text: str) -> bool:
    # Prisma mixes precomposed nukta letters (U+095C/U+095D) with the
    # decomposed base+nukta spelling used below; fold to one form.
    text = text.replace("\u095c", "\u0921\u093c").replace("\u095d", "\u0922\u093c")
    # \w does not cover Devanagari combining marks, so keep the whole block
    # explicit or matras would be stripped ("हाँ" -> "ह").
    normalized = " ".join(
        re.sub(r"[^\w\s\u0900-\u097F]|\u0964|\u0965|\u0970|\u0971", " ", text.lower()).split()
    )
    normalized = " ".join(_DEVANAGARI_WORD_MAP.get(token, token) for token in normalized.split())
    if not normalized or len(normalized) > 160 or "?" in text or re.search(r"\d", normalized):
        return False
    words: list[str] = []
    for token in normalized.split():
        word = _DEVANAGARI_WORD_MAP.get(token, token)
        if not word.isascii() or not word.isalpha():
            # An unmapped word (for example "हम्म" or "लग") means this is not
            # a clean confirmation; dropping it could invert the meaning.
            return False
        words.append(word)
    return any(word in _AFFIRMATIVE_WORDS for word in words) and all(
        word in _CONFIRMATION_WORDS for word in words
    )


