# Milestone 1: Simulated hospital, LLM extraction, risk model

Goal: realistic patients and messy notes, an LLM that reads the notes, and a risk model that decides who the agents contact.

## Pipeline

```
sim.generate -> data.cohort -> extract.social -> extract.evaluate -> model.train -> model.score
```

Run it: `make pipeline` (laptop) or `make pipeline-airflow` (Airflow DAG `m1_build_world`).

## 1. Simulated hospital (`src/readmit/sim/`)

| File | What it does |
|---|---|
| `clock.py` | One calendar for every module: warm-up, history, train/test cutoffs, live period |
| `world.py` | Patients, consent, personas, stays, hidden risk formula, timeline |
| `notes.py` | Messy notes and a record of every social fact each note mentions |
| `generate.py` | Builds it all and loads Postgres |

Key ideas
- Hidden risk formula: each stay gets a true readmission probability; history rolls the dice, live period doesn't
- Live outcomes undecided: the outreach simulation decides them later, so outreach can change outcomes
- Social factors only in notes: lives alone, no caregiver, transportation, health literacy, housing, substance use, medication adherence
- Messy on purpose: abbreviations, inline ICD-10 codes, negations, rule-outs, family history, "trap" sentences about other people, typos, copy-forward, late-signed summaries, duplicate rows
- Leaky note on purpose: post-discharge call written 2-3 days after discharge, reveals the outcome
- Hidden truth in a separate schema (`truth`), never read by the model or agents
- Personas and consent per patient: used by the agents and the patient simulator later

## 2. Cohort (`src/readmit/data/cohort.py`)

- Dedupe on encounter_id
- Label: unplanned admission within 30 days, same patient; planned admissions don't count
- Label before exclusions: a stay ending in death can still be someone's readmission
- Exclude death and hospice
- Time split: train (before Mar 2026), test (Mar to Jul), gap (labels not mature by go-live), live (Sep to Nov, no label)
- Prediction time = discharge + 24h; later notes dropped (removes the leaky call and late summaries)
- Answer key for extraction: factors actually written in the kept notes (`truth.documented`)
- 7 unit tests, one per label rule

## 3. Shared LLM client (`src/readmit/llm.py`)

Used by extraction now and every agent later.

| Feature | Why |
|---|---|
| Fallback model | Primary fails after retries, fallback takes over |
| Retries with backoff | Short outages don't kill a run |
| Rate limiting | Stays under free-tier limits (`LLM_MIN_INTERVAL_S`) |
| Response cache | Same request, no new call; reruns nearly free |
| Schema validation + repair | Output must match a Pydantic schema; one retry showing the model its error |
| LangSmith tracing | Every real call traced with task and prompt version |
| Cost log | Every call in `ops.llm_calls`: tokens, estimated cost, latency, cached, ok, repaired |

Tested with a fake model (no API calls): caching, fallback, repair, total failure.

## 4. Extraction (`src/readmit/extract/`)

- Prompt is a versioned file: `prompts/extract_social_v1.md`
- 6 stays per call: ~320 calls instead of ~1,900
- Grounding check: evidence quote must appear in the notes, or the finding is dropped
- Evaluation against the answer key, logged to MLflow (`note-extraction`)

Result: F1 0.993 overall, every factor 0.98+, 0 ungrounded findings, ~$1 estimated at list price, p50 2.2s per call.

Error analysis: `no_caregiver` precision was 0.92. All 17 false positives quoted one generator phrase ("no one checks on pt daily") that clinically does suggest no caregiver. The label was wrong, not the model. Fixed the data, not the prompt. Precision 0.99. Changing the phrase needed the same word structure, so the random draws (and the whole world) stayed identical.

## 5. Model (`src/readmit/model/`)

| File | What it does |
|---|---|
| `features.py` | One feature builder for train, test and live; fixed category lists; missing-albumin flag; LACE |
| `metrics.py` | AUROC, AUPRC, recall/precision in the top slice, calibration, bootstrap lift test |
| `train.py` | Compares models, logs to MLflow, registers `readmission-risk@champion` |
| `score.py` | Loads the champion by alias, scores live stays: risk, tier, SHAP reasons, barriers, holdout |

Results on the test period

| Model | AUROC | Recall in top 30% |
|---|---|---|
| LACE (clinical standard) | 0.714 | 0.61 |
| Logistic regression | 0.768 | 0.64 |
| XGBoost, structured | 0.801 | 0.69 |
| XGBoost, structured + extracted (champion) | 0.804 | 0.68 |
| Ceiling (true risk) | 0.843 | 0.71 |

- ML beats LACE by ~0.09 AUROC
- Notes lift +0.003, 95% CI [-0.011, +0.018]: not significant. True factors don't help either, so the limit is training size (~1,200 stays), not extraction quality. Extraction's value is operational: agents act on the barriers.
- New patients 0.71 vs repeat 0.79: the model leans on visit history
- Calibrated well at the top quintile (38.7% predicted vs 37.3% observed); pessimistic at the bottom (test base rate lower than train)
- Tiers: cut points fixed from the test distribution (high = top 10%, medium = next 20%)
- Live: 246 discharges, 58 flagged, 46 to outreach, 12 holdout (deterministic hash of patient id)

## Debugging log (worth telling)

- `.gitignore` pattern `data/` hid `src/readmit/data/`; found with `git check-ignore -v`; fixed with `/data/`
- Newer ruff rules in CI than locally; pinned `ruff==0.17.0` so both agree
- Gemini 3 deprecating `temperature`; removed it
- Ambiguous generator phrase mislabeled `no_caregiver`; found by reading the false positives
