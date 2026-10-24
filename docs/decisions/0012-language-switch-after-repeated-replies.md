# 0012 After the question, repeated replies in the other language switch it, with notice

Status: Accepted. Amends [0010](0010-language-is-a-stored-fact.md)

## Context
- ADR 0010: a reply in the other supported language gets one question ("¿Prefiere recibir nuestros mensajes en español? Responda ESPAÑOL."), asked once, never a silent switch
- A patient who ignores the question but keeps writing Spanish got English forever
- People answer the question with "Sí", not "ESPAÑOL"; the rules read a bare "sí" as yes to a visit
- A message in a language the guard can't read (Vietnamese "đau ngực", chest pain) became a routine task, due the next business day

## Decision
- A plain yes ("sí", "claro", "yes please"; not "ok", which acknowledges a booking) to a message that ended with the question switches to that language. Code knows what was just asked
- If the question was asked and the last 3 replies, each 3+ words, are all in the other supported language, the language switches, the turn runs as usual, and code appends a notice with a one-word way back: "Como nos escribe en español, desde ahora le escribiremos en español. Si prefiere inglés, responda ENGLISH." Logged like any change, with the reason. A reply in the patient's own language resets the count
- An interpreter task for a message we couldn't read is urgent (within the hour). An explicit request we could read ("write to me in Vietnamese") stays routine
- Vietnamese typed without accents is detected by common words

## Consequences
- No patient is stuck in a language they don't use; every switch is announced and reversible
- More urgent nurse tasks for unreadable messages: the cost of not missing an emergency written in another language
- Mixed (Spanglish) replies never count toward the 3, so heavy mixers stay in their language on file
