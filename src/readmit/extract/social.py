"""Extract social risk factors from notes with the LLM, then verify every finding against the text.

    python -m readmit.extract.social                 # every stay in train / test / live
    python -m readmit.extract.social --limit 30      # quick check on 30 stays first
    python -m readmit.extract.social --rules         # keyword baseline, no LLM (make demo without a key)

Writes features.social_extracted: one row per stay, 7 true/false factors, the evidence quotes,
and how many findings were thrown out by the grounding check.
"""

import argparse
import json
import re

import pandas as pd
from pydantic import BaseModel

from readmit.llm import LLMError, complete_json, load_prompt
from readmit.sim.notes import SOCIAL_FACTORS

PROMPT_NAME, PROMPT_VERSION = "extract_social_v1", "v1"
TASK = "extract_social"

# Keyword baseline: what a regex extractor would do. Used when there is no API key (make demo), so a fresh clone
# still gets a model. Generic clinical phrases, sentence by sentence; a sentence about a relative, family history,
# or a denial doesn't count. Weaker than the LLM on wording it hasn't seen (make extract-eval compares them).
RULES = {
    "lives_alone": r"\blives? (alone|by (him|her|them)?self)\b",
    "no_caregiver": r"\bno (caregiver|one at home)|\bno (family|friends|family/friends)( nearby)? to help",
    "transport_barrier": r"\bno (car|ride|transport\w*)\b|\bcan'?t afford (a )?(cab|taxi|ride)|\brelies on (the )?bus",
    "low_health_literacy": r"teach-?back (incomplete|failed)|unable to (restate|teach)|\bconfused re\b|"
    r"limited understanding|instructions repeated",
    "housing_unstable": r"\bhomeless|\bshelter\b|\bevict\w*|couch.?surf|sleeping in (a |her |his )?car",
    "substance_use": r"\betoh\b|\bmeth\b|\bcocaine\b|\bheroin\b|\bbeers?\b|\bdrinks? daily|\blast drink",
    "med_nonadherence": r"ran out of (\w+ )?meds|\bnot taking\b|\bskips? (doses|meds)|missed doses|non-?adheren",
}
_NOT_THE_PATIENT = re.compile(
    r"^(pt'?s )?(dtr|daughter|son|husband|wife|partner|brother|sister|mother|father|family)\b|\bfhx\b|"
    r"family h(x|istory)|\b(mother|father) w/",
    re.IGNORECASE,
)
_DENIED = re.compile(r"^(denies|no tobacco|no etoh|no illicit|no drug)|\bsober\b", re.IGNORECASE)


class Factor(BaseModel):
    present: bool
    evidence: str | None = None


class EncounterFactors(BaseModel):
    encounter_id: str
    lives_alone: Factor
    no_caregiver: Factor
    transport_barrier: Factor
    low_health_literacy: Factor
    housing_unstable: Factor
    substance_use: Factor
    med_nonadherence: Factor


class Batch(BaseModel):
    results: list[EncounterFactors]


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def grounded(evidence: str | None, source: str) -> bool:
    """A finding counts only if its quote really appears in that stay's notes (whitespace/case-insensitive)."""
    return bool(evidence) and _norm(evidence) in _norm(source)


def extract_rules(text: str) -> dict:
    """{factor: evidence sentence} for the factors the keyword rules find. The evidence is a sentence of the notes,
    so it passes the same grounding check as the LLM's quotes."""
    found = {}
    for sentence in re.split(r"(?<=[.;])\s+|\n+", text):
        sentence = sentence.strip()
        if not sentence or _NOT_THE_PATIENT.search(sentence) or _DENIED.search(sentence):
            continue
        for factor, pattern in RULES.items():
            if factor not in found and re.search(pattern, sentence, re.IGNORECASE):
                found[factor] = sentence
    return found


def rules_rows(texts: dict[str, str], ids: list[str]) -> list[dict]:
    rows = []
    for eid in ids:
        text = texts.get(eid, "")
        found = {f: e for f, e in extract_rules(text).items() if grounded(e, text)}
        rows.append(
            {
                "encounter_id": eid,
                "extracted": True,
                "ungrounded": 0,
                "evidence": json.dumps(found),
                **{f: f in found for f in SOCIAL_FACTORS},
            }
        )
    return rows


def notes_by_encounter(notes: pd.DataFrame) -> dict[str, str]:
    """Each stay's notes, oldest first, as one text block with a header per note."""
    notes = notes.sort_values(["encounter_id", "note_datetime"])
    out = {}
    for eid, g in notes.groupby("encounter_id"):
        out[eid] = "\n\n".join(
            f"[{r.note_type} | {pd.Timestamp(r.note_datetime):%Y-%m-%d %H:%M}]\n{r.text}" for r in g.itertuples()
        )
    return out


def extract_batch(batch: dict[str, str], system: str) -> list[dict]:
    """One LLM call for several stays. Returns one row per stay, after grounding checks."""
    user = "\n\n".join(f"### ENCOUNTER {eid}\n{text}" for eid, text in batch.items())
    result, _ = complete_json(TASK, PROMPT_VERSION, system, user, Batch)
    by_id = {r.encounter_id: r for r in result.results}
    rows = []
    for eid, text in batch.items():
        r = by_id.get(eid)
        row = {"encounter_id": eid, "extracted": r is not None, "ungrounded": 0, "evidence": {}}
        for f in SOCIAL_FACTORS:
            fac = getattr(r, f) if r else None
            ok = bool(fac and fac.present and grounded(fac.evidence, text))
            if fac and fac.present and not ok:
                row["ungrounded"] += 1  # the model claimed it, but the quote isn't in the notes
            row[f] = ok
            if ok:
                row["evidence"][f] = fac.evidence
        row["evidence"] = json.dumps(row["evidence"])
        rows.append(row)
    return rows


def run(limit: int | None = None, batch_size: int = 6, rules: bool = False) -> pd.DataFrame:
    from readmit import db

    stays = db.read(
        "select encounter_id from cohort.stays where split in ('train','test','live') order by encounter_id"
    )
    ids = stays["encounter_id"].tolist()[:limit] if limit else stays["encounter_id"].tolist()
    notes = db.read(
        "select encounter_id, note_type, note_datetime, text from cohort.notes where encounter_id = any(:ids)", ids=ids
    )
    texts = notes_by_encounter(notes)
    if rules:
        out = pd.DataFrame(rules_rows(texts, ids))
        out["prompt_version"] = "rules"
        db.write(out, "features.social_extracted")
        found = int(out[SOCIAL_FACTORS].to_numpy().sum())
        print(f"\n  keyword rules: {len(out)} stays, {found} findings (no LLM)")
        return out
    system = load_prompt(PROMPT_NAME)

    rows, failed = [], 0
    for i in range(0, len(ids), batch_size):
        batch = {eid: texts.get(eid, "(no notes)") for eid in ids[i : i + batch_size]}
        try:
            rows += extract_batch(batch, system)
        except LLMError as e:
            failed += len(batch)
            print(f"  batch {i // batch_size + 1} failed: {str(e)[:200]}")
            rows += [
                {
                    "encounter_id": eid,
                    "extracted": False,
                    "ungrounded": 0,
                    "evidence": "{}",
                    **{f: False for f in SOCIAL_FACTORS},
                }
                for eid in batch
            ]
        done = min(i + batch_size, len(ids))
        if done % 60 == 0 or done == len(ids):
            print(f"  {done}/{len(ids)} stays")

    out = pd.DataFrame(rows)
    out["prompt_version"] = PROMPT_VERSION
    db.write(out, "features.social_extracted")
    print(
        f"\n  extracted {int(out['extracted'].sum())}/{len(out)} stays, "
        f"{int(out['ungrounded'].sum())} findings rejected by grounding check, {failed} stays failed"
    )
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--batch-size", type=int, default=6)
    ap.add_argument("--rules", action="store_true", help="keyword baseline instead of the LLM (no API key needed)")
    args = ap.parse_args()
    run(args.limit, args.batch_size, args.rules)


if __name__ == "__main__":
    main()
