"""Which language a patient wants, decided by code (ADR 0010). No LLM here.

Three things a reply can tell us:
  1. an explicit request ("can you text me in Spanish", "ESPAÑOL", "I only speak Vietnamese")
     supported language   -> change the stored preference, confirm in the new language
     unsupported language -> a nurse with an interpreter; the AI never writes in a language it hasn't been evaluated in
  2. the reply is written in an unsupported language -> the same interpreter handoff
  3. the reply is written in the other supported language -> keep going, and ask once whether to switch

Never a silent switch: bilingual patients mix languages, and a wrong switch costs them the ability to read us.
Short replies ("ok gracias", "2", "STOP") are never counted as a language: too little text to tell.
"""

import re
import unicodedata

SUPPORTED = ("English", "Spanish")
MIN_WORDS = 3  # fewer words than this: no language decision

# what patients call a language (English and Spanish names) -> the name stored and shown to nurses
NAMES = {
    "english": "English", "ingles": "English",
    "spanish": "Spanish", "espanol": "Spanish", "castellano": "Spanish",
    "vietnamese": "Vietnamese", "vietnamita": "Vietnamese",
    "chinese": "Chinese", "mandarin": "Chinese", "cantonese": "Chinese", "chino": "Chinese",
    "korean": "Korean", "coreano": "Korean",
    "tagalog": "Tagalog", "filipino": "Tagalog",
    "arabic": "Arabic", "arabe": "Arabic",
    "russian": "Russian", "ruso": "Russian",
    "portuguese": "Portuguese", "portugues": "Portuguese",
    "french": "French", "frances": "French",
    "farsi": "Persian", "persian": "Persian",
    "armenian": "Armenian", "armenio": "Armenian",
    "hindi": "Hindi", "punjabi": "Punjabi",
    "japanese": "Japanese", "japones": "Japanese",
}  # fmt: skip
_NAMES = "|".join(sorted(NAMES, key=len, reverse=True))
REQUEST = [  # matched on lowercased text with accents removed
    rf"^\W*({_NAMES})\W*(please|por favor)?\W*$",  # the whole reply is a language: "ESPAÑOL", "Spanish please"
    rf"\b(in|en)\s+({_NAMES})\b",  # "text me in Spanish", "en ingles por favor"
    rf"\b(speak|speaks|read|reads|habl[oa]|le[oe])\s+(only\s+|solo\s+|solamente\s+)?({_NAMES})\b",  # "I only speak..."
    rf"\bonly\s+(speak|read)\s+({_NAMES})\b",
    rf"\b(switch|change|cambi\w*)\s+(to|a|al)\s+({_NAMES})\b",  # "switch to Spanish", "cambie a ingles"
    rf"\b(prefer|prefiero)\s+({_NAMES})\b",
]

# words common in one language and rare in the others (shared words like "no", "me", "a" are left out)
STOPWORDS = {
    "English": {
        "the", "and", "is", "are", "you", "my", "it", "for", "can", "that", "this", "what", "with", "have", "not",
        "your", "thanks", "thank", "please", "yes", "work", "works", "one", "first", "time", "will", "would", "i", "do",
        "of", "to", "be", "there", "need", "want",
    },
    "Spanish": {
        "el", "la", "los", "las", "de", "que", "y", "en", "es", "por", "para", "con", "mi", "un", "una", "su", "lo",
        "puedo", "quiero", "gracias", "si", "esta", "hola", "bien", "primera", "primero", "cita", "usted", "tengo",
        "necesito", "hora", "favor", "pero", "como", "cuando", "donde", "opcion", "funciona", "manana",
    },
    "Portuguese": {"voce", "obrigado", "obrigada", "nao", "sim", "estou", "tambem", "muito", "eu", "meu", "minha", "consulta"},
    "French": {"je", "vous", "merci", "oui", "pas", "suis", "avec", "pour", "mon", "rendez", "bonjour", "est"},
    "Tagalog": {"ako", "po", "salamat", "hindi", "ang", "ng", "mga", "sa", "ko", "opo", "kailangan", "ninyo", "ito"},
}  # fmt: skip
SCRIPTS = [  # (unicode range, language): a reply written in this script is in this language
    (r"[぀-ヿ]", "Japanese"),  # kana first: Japanese also uses Chinese characters
    (r"[가-힯]", "Korean"),
    (r"[一-鿿]", "Chinese"),
    (r"[؀-ۿ]", "Arabic"),
    (r"[Ѐ-ӿ]", "Russian"),
    (r"[԰-֏]", "Armenian"),
    (r"[ऀ-ॿ]", "Hindi"),
    (r"[਀-੿]", "Punjabi"),
]
VIETNAMESE = re.compile(r"[ăđơưạảấầẩẫậắằẳẵặẹẻẽếềểễệỉịọỏốồổỗộớờởỡợụủứừửữựỳỵỷỹ]", re.IGNORECASE)

QUESTION = {  # appended by code, once per patient, when they write in the other supported language
    "Spanish": "¿Prefiere recibir nuestros mensajes en español? Responda ESPAÑOL.",
    "English": "Would you prefer our messages in English? Reply ENGLISH.",
}


def fold(text: str | None) -> str:
    """Lowercase, accents removed: "Español" -> "espanol"."""
    t = unicodedata.normalize("NFKD", (text or "").lower())
    return "".join(ch for ch in t if not unicodedata.combining(ch))


def requested(text: str | None) -> str | None:
    """The language the patient explicitly asks for, or None."""
    t = fold(text)
    for pattern in REQUEST:
        if m := re.search(pattern, t):
            word = next(g for g in reversed(m.groups()) if g and g.strip() in NAMES)
            return NAMES[word.strip()]
    return None


def detect(text: str | None) -> str | None:
    """The language a reply is written in, or None when it's too short or too mixed to tell."""
    raw = text or ""
    for pattern, language in SCRIPTS:
        if len(re.findall(pattern, raw)) >= 2:
            return language
    if len(VIETNAMESE.findall(raw)) >= 2:
        return "Vietnamese"
    words = re.findall(r"[a-z]+", fold(raw))
    if len(words) < MIN_WORDS:
        return None
    scores = {lang: sum(w in vocab for w in words) for lang, vocab in STOPWORDS.items()}
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    (best, top), (_, second) = ranked[0], ranked[1]
    if top >= 2 and top >= 2 * second:
        return best
    return None


def screen(text: str | None, preferred: str, asked: bool) -> dict:
    """What to do about language for one reply. Run only after STOP, emergencies, third parties and the phone
    identity check: those come first whatever the language.

    {'action': 'language_change', 'language': ...}   explicit request for the other supported language
    {'action': 'interpreter', 'language': ...}       request for, or reply in, an unsupported language
    {'action': 'continue', 'ask': ...}               written in the other supported language: ask once
    {'action': 'continue'}                           nothing to do
    """
    want = requested(text)
    if want and want != preferred:
        return {"action": "language_change" if want in SUPPORTED else "interpreter", "language": want}
    wrote = detect(text)
    if wrote and wrote not in SUPPORTED:
        return {"action": "interpreter", "language": wrote}
    if wrote and wrote != preferred and not asked and not want:
        return {"action": "continue", "ask": wrote}
    return {"action": "continue"}
