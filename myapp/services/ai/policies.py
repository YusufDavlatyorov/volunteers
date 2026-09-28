"""Deterministic, pre-LLM scope guard.

``classify_message`` recognises the clearest out-of-scope / adversarial requests
with multilingual patterns so the assistant can refuse them *without* calling
Groq — which makes the boundary cheap and unit-testable. It is deliberately
conservative: the system prompt (``prompts.system_prompt``) is the second layer
for everything these patterns miss, and an ambiguous message is passed through
rather than wrongly blocked.

Categories: "injection" | "medical" | "political" | "off_topic" | None
"""

from __future__ import annotations

import re
from typing import Optional

# --- prompt injection / instruction-override / secret extraction ---------------
_INJECTION = [
    r"ignore (all |any |the )?(your |our )?(previous|prior|above|earlier|preceding|initial|system)\b.*\b(instruction|prompt|rule|message|direction)",
    r"disregard (all |any |the )?(previous|prior|above|earlier|your)\b",
    r"forget (all |everything|your |the )?(you were told|previous|prior|instruction|rule|context)",
    r"(purge|override|bypass|reset)\s+(your\s+)?(instruction|rule|prompt|guardrail|polic)",
    r"\b(developer|dev|god|admin|sudo|jailbreak|dan)\s*mode\b",
    r"pretend (to be|you(?:'re| are)| that you)",
    r"\bact as\b.*\b(admin|administrator|superuser|developer|different)",
    r"you\s+(are|will be|become)\s+(now\s+)?(an?\s+)?(admin|administrator|superuser|root|curator)\b",
    r"give (me )?(admin|superuser|full|root) (access|rights|permission)",
    r"(show|print|reveal|display|give|tell)\s+(me\s+)?(your\s+)?(the\s+)?(system\s+)?(prompt|instructions|guidelines|rules you)",
    r"what (is|are) your (system )?(prompt|instructions|initial instructions|rules)",
    r"(show|print|reveal|leak|dump|give)\s+(me\s+)?(the\s+)?(password|api[ _-]?key|secret|token|env|credential|\.env)",
    r"(execute|run|perform)\s+(this\s+)?(raw\s+)?sql\b",
    r"\bdrop table\b|\bdelete from \w+|\bselect .* from \w+ where",
    # Russian
    r"игнорируй (все |свои |предыдущие |прошлые |приведённые )?(инструкц|правил|указан|сообщен|промпт)",
    r"забудь (всё|все|свои|предыдущие|прошлые|инструкц|правил|о чём)",
    r"(ты|вы)\s+(теперь|сейчас|отныне)\s+(админ|администратор|суперпользователь|разработчик|куратор|root)",
    r"притвор(ись|яйся|итесь)|представь,?\s*что ты",
    r"(покажи|выведи|скажи|назови|дай|раскрой|напиши)\s+(мне\s+)?(свой |системн\w+ |твой |внутренн\w+ )*(промпт|инструкц|систем\w* сообщен|правила которые|guidelines)",
    r"(какой|какие|что за)\s+(у тебя\s+)?(системн\w+\s+)?(промпт|инструкц)",
    r"(покажи|выведи|скажи|назови|дай|раскрой)\s+(мне\s+)?(пароль|api[ _-]?ключ|секрет|токен|ключ api|переменны\w* окружени)",
    r"(выполни|запусти|сделай)\s+(этот\s+)?sql\b",
    r"режим разработчика|режим бога|джейлбрейк",
    # Tajik
    r"дастур(ҳо|ҳоятро|и қаблӣ)ро нодида",
    r"ту (акнун|ҳоло) администратор",
    r"(промпт|дастур)и системиро нишон",
]

# --- medical advice (diagnosis / medication / dosage / treatment / symptoms) ---
# Advice-seeking shapes only. Errand phrasing ("help me buy medicine",
# "pick up a prescription") has none of these and is left for the LLM to treat
# as a normal grocery/transport request.
_MEDICAL = [
    r"(what|which)\s+(medicine|medication|drug|pill|tablet|antibiotic|painkiller|remedy)\b",
    r"(what|which|how much|how many)\b.*\b(dose|dosage|mg|milligram)\b",
    r"\b(dosage|what dose|correct dose|how much to take|how many pills)\b",
    r"(should|can|could)\s+(he|she|they|i|we|the patient|my (mother|father|client|grandmother|grandfather))\s+take\b",
    r"\b(diagnos(e|is|ing)|what('?s| is) wrong with (him|her|them|me))\b",
    r"\b(prescribe|prescription for|treat(ment)? for|how to treat|cure for)\b",
    r"\bwhat (should|do) (i|we|they) do (about|for|if).*(fever|pain|cough|pressure|dizziness|bleeding|symptom|sick|ill)\b",
    r"\b(is|are) (this|these|the) symptom",
    # Russian
    r"(какое|какой|какую|какие|что за)\s+(лекарств|препарат|таблетк|антибиотик|обезболив|мазь|капл|сироп|доз)",
    r"(лекарств\w*|препарат\w*|таблетк\w*)\s+.{0,25}(посовет|порекоменд|подобрат|выбрат|дать|принимать|пить|назнач)",
    r"(посовет\w*|порекоменд\w*|подскаж\w*)\s+.{0,25}(лекарств|препарат|таблетк|обезболив|дозир|дозу)",
    r"(какая|какую|сколько)\s+.{0,15}(доз|дозировк)",
    r"\b(дозировк\w*|дозу\b|какую дозу|сколько таблеток|сколько раз в день пить)\b",
    r"чем\s+(лечить|лечиться)|как\s+(лечить|вылечить|лечиться)|чем\s+сбить\s+(температур|давлени)",
    r"(поставь|какой|определи|скажи)\s+.{0,10}диагноз",
    r"(это\s+)?опасн\w+\s+(ли\s+)?(эти\s+)?симптом|что за симптом|какие симптомы|это симптомы чего",
    r"что\s+(принять|выпить|дать|делать)\s+(от|при)\s+(температур|боли|давлени|кашл|головн|горл|живот|сердц)",
    # Tajik
    r"(кадом|чӣ)\s+дору",
    r"миқдори\s+дору|чанд ҳаб(и|)\s+хӯр|ташхис (гузор|чист)",
]

# --- politics / elections / ideology ------------------------------------------
_POLITICAL = [
    r"\bpolitic(s|al|ian)\b",
    r"\bwho (should|do|would) (i|we|you) vote\b|\bvote for\b|\bwhich (party|candidate)\b",
    r"\belection(s)?\b|\bpresidential race\b|\bparliament(ary)?\b|\bgeopolit",
    r"\bwhat do you think about (the )?(government|president|war|politics)\b",
    # Russian
    r"\bполитик[аеиу]?\b|политическ|политикан",
    r"за кого\s+(мне\s+)?(голосова|проголосова)|кого\s+выбрать\s+на\s+выбор|как\s+голосова",
    r"\bвыбор(ы|ах|ов)\b|предвыборн|президентск\w+\s+(гонк|выбор|кампан)|парламентск",
    r"\b(оппозици|депутат|правящ\w+ партия|полит\w* партия)\b",
    r"(кто|что)\s+(победит|лучше)\s+.{0,20}(выбор|политик)|что\s+ты\s+думаешь\s+о\s+политик",
    # Tajik
    r"\bсиёсат|интихобот\b|ба кӣ овоз",
]

# --- clearly off-topic general-assistant requests -----------------------------
_OFF_TOPIC = [
    r"\bwrite (me )?(some |a )?(python|javascript|java|c\+\+|code|program|script|function|regex|sql query)\b",
    r"\b(do|solve|finish) (my )?homework\b|\bhomework (help|assignment)\b",
    r"\b(crypto(currency)?|bitcoin|ethereum|forex|stock market)\b.*\b(invest|buy|earn|profit|trade)\b",
    r"\bhow (to|do i) (make|earn) money\b|\bget rich\b",
    r"\bhow (to|do i) (start|open|register) (a |my )?(business|company|shop)\b",
    r"\b(write|compose) (me )?(an? )?(essay|poem|song|story|joke)\b",
    # Russian
    r"напиши\s+(мне\s+)?(код|программ|скрипт|функци|на\s+python|на\s+джаваскрипт|на\s+c\+\+|регулярк|sql[- ]запрос)",
    r"(сделай|реши|напиши)\s+.{0,15}(домашн\w+\s+задани|домашк|контрольн|курсов)",
    r"\bкриптовалют|биткоин|эфириум\b.{0,30}(куп|инвест|заработ|вложить|торгов)",
    r"как\s+(за)?работать\s+.{0,15}(деньг|миллион|на\s+крипт|на\s+бирж)|как\s+разбогате",
    r"как\s+(открыть|начать|зарегистрировать)\s+(свой\s+)?(бизнес|компани|магазин|ип)",
    r"напиши\s+(мне\s+)?(эссе|стих|песн|рассказ|сочинени|анекдот|шутк)",
]


def _compile(patterns):
    return [re.compile(p, re.IGNORECASE) for p in patterns]


_RULES = [
    ("injection", _compile(_INJECTION)),
    ("medical", _compile(_MEDICAL)),
    ("political", _compile(_POLITICAL)),
    ("off_topic", _compile(_OFF_TOPIC)),
]


def classify_message(text: str) -> Optional[str]:
    """Return the out-of-scope category for ``text``, or ``None`` to pass it on."""
    if not text:
        return None
    normalized = re.sub(r"\s+", " ", text).strip().lower()
    for label, patterns in _RULES:
        for pattern in patterns:
            if pattern.search(normalized):
                return label
    return None
