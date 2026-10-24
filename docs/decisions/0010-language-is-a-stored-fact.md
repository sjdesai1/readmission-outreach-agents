# 0010 Language is a stored fact decided by code; unsupported languages go to a person

Status: Accepted

## Context
- The writer used the language on the registration record and nothing else. A patient who asked "can you write in Spanish?" got an answer in English, every time
- Bilingual patients mix languages, so a reply in Spanish doesn't mean the patient wants Spanish
- STOP was only recognized in English; Spanish speakers write PARAR, BASTA, NO MÁS
- The AI is evaluated in English and Spanish only. A message in another language, written by a model, has never been checked by anyone who reads that language: an untested clinical message is a safety risk

## Decision
- An explicit request ("text me in Spanish", "ESPAÑOL", "I only speak Vietnamese") is caught by rules in the input guard, after STOP, emergencies, third parties and the phone identity check. The LLM interpreter has a `language_change` intent as a second net
- A supported language is stored in patient memory through `set_language` (audited, patient-level, overrides the record). The reply confirms the switch and offers the times again in the new language; a booked patient just gets the confirmation
- A reply written in the other supported language is answered as usual, plus one question: "Would you prefer our messages in English? Reply ENGLISH." Asked once per patient, recorded in memory. Never a silent switch
- A request for, or a reply in, any other language creates a nurse task with an interpreter flag. The patient gets an acknowledgement in the language on file; the nurse keeps the case
- Language detection is code: Unicode scripts, Vietnamese letters, and stopword counts. Replies under three words don't count
- Spanish opt-out words join the STOP list, matched on the whole message with accents removed. "Para" alone is left out: it also means "for"
- The nurse screen shows a machine translation under every message not in English, labeled as such, and the interpreter flag

## Consequences
- Patients read every message in the language they chose, and say so once
- A Spanish reply from an English-record patient adds about 70 characters to one message
- Interpreter cases cost nurse time; the AI does nothing for them after the acknowledgement
- Short or mixed replies are never treated as a language signal: some genuine preferences wait for an explicit request
- Detection by stopwords misses languages with no listed words; an unknown language reads as "no decision", and the interpreter handles the reply as usual
- Adding a language means: templates in the rule-based writer, Spanish-style date formats in the output checks, eval cases, then adding it to SUPPORTED. Not a prompt change alone
