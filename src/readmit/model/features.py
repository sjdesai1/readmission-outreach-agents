"""Model inputs. The same function builds features for training, testing and live scoring,
so columns always line up (a classic source of silent production bugs)."""
import pandas as pd

from readmit.sim.notes import SOCIAL_FACTORS

NUMERIC = ["age", "adi_decile", "length_of_stay_days", "charlson_index", "prior_admissions_12m", "ed_visits_6m",
           "prior_30d_readmits_12m", "hemoglobin", "sodium", "creatinine", "albumin", "discharge_sbp",
           "num_discharge_meds", "med_changes_count"]
BOOLEAN = ["planned_admission", "icu_stay", "chf", "copd", "diabetes", "ckd", "cancer", "depression", "dementia",
           "high_risk_med", "followup_scheduled_7d"]
CATEGORIES = {   # fixed category lists: a category unseen in training can't create a new column at scoring time
    "sex": ["F", "M"],
    "payer": ["Medicare", "Medicare Advantage", "Medicaid", "Commercial", "Self-pay"],
    "admission_type": ["Emergency", "Urgent", "Elective"],
    "primary_dx_group": ["Heart failure", "COPD exacerbation", "Pneumonia", "Sepsis", "Diabetes complication",
                         "Acute MI", "Acute kidney injury", "GI bleed", "Cellulitis", "Other"],
    "discharge_disposition": ["Home", "Home health", "SNF", "Against medical advice"],
}
VARIANTS = {
    "structured": None,                    # what's in structured fields at discharge
    "structured+extracted": "extracted",   # + social factors the LLM pulled from notes
    "structured+true": "true",             # + the documented truth: upper bound for perfect extraction
}


def build(stays: pd.DataFrame, social: pd.DataFrame | None = None) -> pd.DataFrame:
    """One row per stay, indexed by encounter_id. `social` adds the 7 note-derived flags."""
    X = stays.set_index("encounter_id")
    out = X[NUMERIC].astype(float)
    out["albumin_missing"] = X["albumin"].isna().astype(int)        # not ordered = a signal in itself
    for c in BOOLEAN:
        out[c] = X[c].astype(bool).astype(int)
    for c, cats in CATEGORIES.items():
        dummies = pd.get_dummies(pd.Categorical(X[c], categories=cats), prefix=c, dtype=int)
        dummies.index = X.index
        out = out.join(dummies)
    if social is not None:
        s = social.set_index("encounter_id")[SOCIAL_FACTORS].astype(bool).astype(int)
        out = out.join(s.add_prefix("note_"), how="left").fillna({f"note_{f}": 0 for f in SOCIAL_FACTORS})
    return out


def lace(stays: pd.DataFrame) -> pd.Series:
    """LACE index, the standard clinical readmission score: Length of stay, Acuity, Comorbidity, ED visits."""
    los = stays["length_of_stay_days"]
    L = pd.cut(los, [-1, 0, 1, 2, 3, 6, 13, 10_000], labels=[0, 1, 2, 3, 4, 5, 7]).astype(int)
    A = stays["admission_type"].isin(["Emergency", "Urgent"]).astype(int) * 3
    C = stays["charlson_index"].clip(upper=4).map({0: 0, 1: 1, 2: 2, 3: 3, 4: 5})
    E = stays["ed_visits_6m"].clip(upper=4)
    return pd.Series((L + A + C + E).to_numpy(), index=stays["encounter_id"], name="lace")
