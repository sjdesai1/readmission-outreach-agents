# Milestone 4: re-scoring after discharge

The discharge score is a snapshot. New information arrives in the 30 days after: prescriptions filled or not, ED visits, a follow-up kept, how the patient responds to outreach. This step re-scores each case as that information arrives.

## How a case gets re-scored

| Trigger | When | What happens |
|---|---|---|
| ED visit (sent home) | an hour after it happens | re-score; urgent nurse callback if the risk rises 5+ points or reaches 50% |
| No prescription fill | day 3 | re-score; routine callback under the same rule |
| Landmarks | day 7, 14, 21 | re-score only: a safety net that reorders outreach |

- Event before the case opens (first 24h): handled when it opens.
- Visit already done: still re-scored and alerted. The callback does not restart outreach.
- Readmitted or opted out: no re-score, no call.

## The model: update the prior, don't start over

```
logit(risk now) = a + b * logit(discharge risk) + c1 * rx_missing + c2 * rx_late + c3 * ed_visits + c4 * followup_kept
```

- 6 numbers per landmark, saved as JSON with odds ratios. Trained on past stays still out of the hospital at each landmark (the landmark method).
- First version refit the full XGBoost model per landmark. It overfit the few readmissions left after day 7 and made things worse (day 14: -0.022 AUROC, CI excluding zero).
- Second bug: "days to fill", censored at the landmark, meant different things on different days. A day-7 model applied on day 3 read a missed fill as good news. Fixed with yes/no signals and a day-3 model.

Results, 5-fold CV grouped by patient, past stays:

| Landmark | Readmissions left | AUROC prior | + signals | Lift (95% CI) |
|---|---|---|---|---|
| day 3 | 216 | 0.765 | 0.789 | +0.025 [+0.010, +0.040] |
| day 7 | 139 | 0.746 | 0.789 | +0.043 [+0.014, +0.074] |
| day 14 | 60 | 0.746 | 0.728 | -0.018 [-0.038, +0.002] |
| day 21 | 26 | 0.766 | 0.775 | +0.008 [-0.022, +0.038] |

- An ED visit multiplies the odds of readmission by 12 to 29. It shows up days before a readmission, so it matters most early.
- A missed fill alone barely moves risk (odds ratio about 1.2). It re-scores but rarely alerts. The data set that threshold, not a rule of thumb.
- Days 14 and 21: too few readmissions left to measure anything.

## Outreach signals and the cold start (step 7b)

Outreach behavior (replied, silent, booked, no-show, barrier, unreachable) has no history before the program starts. The demo has 46 cases and about 9 readmissions: nothing to learn from.

Approach: `make program` builds a 20x hospital in its own database, scores it with the champion, runs 4 months of outreach with the rule-based agents. About 1,000 cases, about 250 readmissions. No LLM calls (note extraction simulated at the real extractor's error rates).

### Run 1: main world rules

| Landmark | Lift from all 6 outreach signals (95% CI) |
|---|---|
| day 7 | -0.015 [-0.033, +0.002] |
| day 14 | +0.003 [-0.034, +0.033] |

Null. In the main world, a patient's persona (engaged, silent, ...) is drawn independently of their risk. Outreach behavior can't predict what the world never linked to it. A simulation only reveals what it encodes.

### Run 2: positive control

World option: disengaged patients carry real extra risk (silent +0.8 log-odds, about 2.2x odds). The main world is unchanged.

Day 7, 893 cases at risk, 148 readmissions:

| Model | AUROC | Lift (95% CI) | Log-loss gain (95% CI) |
|---|---|---|---|
| prior + world signals | 0.704 | | |
| + replied, silent (pre-specified) | 0.715 | +0.010 [-0.006, +0.028] | +0.0041 [-0.0042, +0.0117] |
| + all 6 outreach signals | 0.712 | +0.008 [-0.012, +0.026] | +0.0005 [-0.0085, +0.0088] |
| + true engagement risk (oracle) | 0.716 | +0.012 [-0.005, +0.027] | +0.0048 [-0.0018, +0.0113] |

Learning curve, pre-specified signals, 5 random subsets per size:

| Cases | Readmissions | AUROC lift | Subsets where log-loss improved |
|---|---|---|---|
| 100 | ~17 | -0.006 | 2/5 |
| 200 | ~33 | +0.015 | 5/5 |
| 400 | ~66 | +0.010 | 4/5 |
| 800 | ~133 | +0.013 | 5/5 |

What it shows:
- The pipeline works. "silent" came back with an odds ratio of 2.5 against a true 2.2.
- The two pre-specified signals capture most of what is possible (+0.010 of an oracle +0.012).
- What is possible is small. A 15% subgroup with 2x odds moves ranking about one AUROC point. Not significant at 1,000 cases.
- Fishing costs. All 6 signals did worse than 2; at day 14 (67 readmissions) they hurt significantly (-0.051).
- Day 14: even the oracle adds nothing. The silent, high-risk patients were mostly readmitted before day 14.

## Decisions

- Live re-score: prior + pharmacy fills, ED visits, kept visits. Event triggers for ED visits and missed fills.
- Outreach signals: not in the live model yet. Pre-specified test (replied, silent, day 7 only), rerun as cases accumulate. Promote when the CI excludes zero, expected around 3,000 to 4,000 cases at this effect size.
- A real program would check the premise first: in real data, does disengagement predict readmission at all? The simulation can't answer that.

## Run it

```bash
make signals && make landmark            # past-stay signals, update models
make outreach-setup && make simulate days=90
make program                             # 20x program, main world rules (~1 hour, no LLM)
make program control=1                   # positive control
make program-eval
```
