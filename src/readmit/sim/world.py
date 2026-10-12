"""The simulated world: patients, their hospital stays over time, and the hidden truth.

How readmissions happen (the hidden policy the model has to discover):
  * every patient has a frailty level, chronic conditions and social circumstances
  * every stay gets a TRUE readmission probability from `risk_logit`
  * some drivers are structured columns; the social ones exist only in notes
  * history period: after each discharge the dice are rolled; a readmission adds a stay 1-30 days later
  * live period: the dice are NOT rolled here. Outcomes are decided later by the outreach
    simulation, so outreach can change them.

Pure functions returning DataFrames. No database code here, so it is easy to test.
"""
import math
from datetime import timedelta

import numpy as np
import pandas as pd

from readmit.sim import clock
from readmit.sim.notes import NoteWriter

BASE_LOGIT = -3.70   # sets the overall readmission rate (~15%)

FIRST_F = ["Maria", "Linh", "Susan", "Patricia", "Ana", "Mei", "Linda", "Rosa", "Karen", "Grace",
           "Elena", "Barbara", "Thi", "Carmen", "Joyce", "Helen", "Lucia", "Nancy", "Diane", "Teresa"]
FIRST_M = ["Jose", "Robert", "Minh", "James", "Luis", "David", "John", "Wei", "Carlos", "Michael",
           "Tuan", "Richard", "Juan", "William", "Thomas", "Daniel", "Hung", "Gary", "Miguel", "Paul"]
LAST = ["Garcia", "Nguyen", "Smith", "Martinez", "Tran", "Johnson", "Lopez", "Chen", "Williams", "Hernandez",
        "Le", "Brown", "Gonzalez", "Pham", "Davis", "Rodriguez", "Wang", "Miller", "Perez", "Kim", "Wilson",
        "Sanchez", "Lee", "Anderson", "Ramirez", "Park", "Taylor", "Flores"]
RACE = (["White", "Hispanic or Latino", "Asian", "Black or African American", "Other or Unknown"],
        [0.38, 0.30, 0.19, 0.07, 0.06])

# How each patient will behave when the agents reach out (used by the conversation simulator later).
PERSONAS = {"engaged": 0.32, "busy": 0.18, "silent": 0.15, "med_question": 0.09, "symptomatic": 0.07,
            "opt_out": 0.06, "no_ride": 0.05, "injection_attempt": 0.04, "abusive": 0.04}

DX = {  # primary diagnosis group -> (base weight, typical length of stay, ICU chance)
    "Heart failure": (1.0, 4.5, 0.08), "COPD exacerbation": (0.8, 3.8, 0.07), "Pneumonia": (1.2, 4.2, 0.10),
    "Sepsis": (1.0, 6.0, 0.30), "Diabetes complication": (0.6, 3.5, 0.10), "Acute MI": (0.5, 3.8, 0.25),
    "Acute kidney injury": (0.6, 4.0, 0.06), "GI bleed": (0.6, 3.6, 0.15), "Cellulitis": (0.7, 3.4, 0.02),
    "Other": (1.5, 3.2, 0.05),
}
ELECTIVE_DX = ["Other", "Acute MI", "GI bleed"]
CHRONIC = ["chf", "copd", "diabetes", "ckd", "cancer", "depression", "dementia"]


def sigmoid(x):
    return 1.0 / (1.0 + np.exp(-x))


class World:
    def __init__(self, n_patients=1000, seed=7):
        self.n = n_patients
        self.rng = np.random.default_rng(seed)
        self.writer = NoteWriter(self.rng)

    def chance(self, p):
        return bool(self.rng.random() < p)

    def pick(self, options):
        return options[int(self.rng.integers(len(options)))]

    # ------------------------------------------------------------------ patients
    def make_patient(self, i):
        sex = self.pick(["F", "M"])
        first, last = self.pick(FIRST_F if sex == "F" else FIRST_M), self.pick(LAST)
        age0 = float(np.clip(self.rng.normal(64, 16), 18, 96))     # age at WARMUP_START
        adi = int(self.rng.integers(1, 11))                          # area deprivation decile, 10 = most deprived
        lang = "Spanish" if self.chance(0.17) else "English"
        frailty = float(self.rng.normal(0, 1))

        if age0 >= 65:
            payer = self.rng.choice(["Medicare", "Medicare Advantage", "Medicaid"], p=[0.55, 0.33, 0.12])
        else:
            p_mcd = 0.15 + 0.04 * adi
            payer = self.rng.choice(["Commercial", "Medicaid", "Self-pay"], p=[0.92 - p_mcd, p_mcd, 0.08])

        a = (age0 - 65) / 10
        chronic = {
            "chf": self.chance(sigmoid(-2.0 + 0.45 * a + 0.6 * frailty)),
            "copd": self.chance(sigmoid(-2.2 + 0.25 * a + 0.4 * frailty)),
            "diabetes": self.chance(sigmoid(-1.0 + 0.10 * a + 0.3 * frailty)),
            "ckd": self.chance(sigmoid(-2.0 + 0.40 * a + 0.5 * frailty)),
            "cancer": self.chance(sigmoid(-2.6 + 0.30 * a + 0.3 * frailty)),
            "depression": self.chance(sigmoid(-1.9 + 0.3 * frailty)),
            "dementia": self.chance(sigmoid(-4.2 + 0.90 * a + 0.4 * frailty)),
        }
        lives_alone = self.chance(0.17 + 0.13 * (age0 >= 75))
        social = {
            "lives_alone": lives_alone,
            "has_caregiver": (not lives_alone) or self.chance(0.30),
            "transport_barrier": self.chance(0.04 + 0.022 * adi),
            "low_health_literacy": self.chance(0.08 + 0.01 * adi + 0.20 * chronic["dementia"]),
            "housing_unstable": self.chance(0.005 + 0.005 * adi + 0.05 * (payer in ("Medicaid", "Self-pay"))),
            "substance_use": self.chance(0.05 + 0.06 * (age0 < 55)),
        }
        social["med_nonadherence"] = self.chance(0.10 + 0.08 * social["transport_barrier"]
                                                 + 0.08 * social["low_health_literacy"] + 0.10 * social["substance_use"])

        # persona: language and barriers make some personas more likely
        if lang == "Spanish" and self.chance(0.6):
            persona = "spanish_speaker"
        elif social["transport_barrier"] and self.chance(0.6):
            persona = "no_ride"
        else:
            names, probs = list(PERSONAS), np.array(list(PERSONAS.values()))
            persona = names[int(self.rng.choice(len(names), p=probs / probs.sum()))]

        # contact details use reserved fake ranges: example.com and 555-01xx numbers
        has_email = self.chance(0.85 - 0.004 * max(0, age0 - 60))
        has_mobile = self.chance(0.93)
        has_portal = self.chance(0.70 - 0.005 * max(0, age0 - 60))
        consent = {
            "portal_opt_in": has_portal and self.chance(0.90),
            "sms_opt_in": has_mobile and self.chance(0.60),
            "email_opt_in": has_email and self.chance(0.60),
            "phone_opt_in": self.chance(0.85),
            "telehealth_opt_in": self.chance(0.55 - 0.004 * max(0, age0 - 60)),
        }
        opted = [c.removesuffix("_opt_in") for c, v in consent.items() if v and c != "telehealth_opt_in"]
        pid = f"P{i:05d}"
        patient = {
            "patient_id": pid, "first_name": first, "last_name": last,
            "birth_year": int(clock.WARMUP_START.year - age0), "sex": sex,
            "race_ethnicity": self.rng.choice(RACE[0], p=RACE[1]),   # fairness audits only, never a model input
            "preferred_language": lang, "payer": payer, "adi_decile": adi,
            "pcp_assigned": self.chance(0.80 - 0.30 * (payer == "Self-pay")),
            "email": f"{first.lower()}.{last.lower()}{i}@example.com" if has_email else None,
            "mobile_phone": f"+1714555{int(self.rng.integers(100, 200)):04d}" if has_mobile else None,
            "has_portal_account": has_portal,
            **consent,
            "telehealth_capable": self.chance(0.75 - 0.006 * max(0, age0 - 60)),
            "preferred_channel": self.pick(opted) if opted else None,
            "contact_window": self.pick(["morning", "afternoon", "evening"]),
        }
        hidden = {"patient_id": pid, "age0": round(age0, 1), "frailty": round(frailty, 3),
                  **chronic, **social, "persona": persona}
        return patient, hidden

    # ------------------------------------------------------------------ stays
    def choose_dx(self, h, planned):
        if planned:
            return self.pick(ELECTIVE_DX)
        w = {k: v[0] for k, v in DX.items()}
        w["Heart failure"] *= 4 if h["chf"] else 0.4
        w["COPD exacerbation"] *= 4 if h["copd"] else 0.2
        w["Diabetes complication"] *= 2.5 if h["diabetes"] else 0.1
        w["Acute kidney injury"] *= 2 if h["ckd"] else 0.8
        keys = list(w)
        p = np.array([w[k] for k in keys])
        return keys[int(self.rng.choice(len(keys), p=p / p.sum()))]

    @staticmethod
    def charlson(h, age):
        pts = h["chf"] + h["copd"] + h["diabetes"] + 2 * h["ckd"] + 2 * h["cancer"] + h["dementia"]
        return int(pts + (0 if age < 50 else 1 if age < 60 else 2 if age < 70 else 3 if age < 80 else 4))

    @staticmethod
    def risk_logit(e, h):
        """THE HIDDEN POLICY. The model never sees this function, only its consequences."""
        def ok(v):
            return not math.isnan(v)   # False for a missing lab
        x = BASE_LOGIT
        x += 0.06 * min(e["length_of_stay_days"] - 4, 10)
        x += 0.12 * (e["charlson_index"] - 4)
        x += 0.35 * (e["admission_type"] == "Emergency")
        x += 0.22 * min(e["ed_visits_6m"], 4) + 0.28 * min(e["prior_admissions_12m"], 4)
        x += 0.45 * min(e["prior_30d_readmits_12m"], 2)
        x += 0.35 * e["chf"] + 0.25 * e["copd"] + 0.20 * e["ckd"] + 0.30 * e["cancer"] + 0.20 * e["depression"]
        x += -0.35 * e["followup_scheduled_7d"] + 0.30 * (e["chf"] and not e["followup_scheduled_7d"])
        x += {"Home health": -0.10, "SNF": 0.25, "Against medical advice": 0.70}.get(e["discharge_disposition"], 0.0)
        x += 0.40 * (ok(e["hemoglobin"]) and e["hemoglobin"] < 10)
        x += 0.30 * (ok(e["sodium"]) and e["sodium"] < 133)
        x += 0.30 * (ok(e["creatinine"]) and e["creatinine"] > 2.0)
        x += 0.35 * (ok(e["albumin"]) and e["albumin"] < 3.0)
        x += 0.03 * (e["num_discharge_meds"] - 10) + 0.25 * e["high_risk_med"] + 0.08 * min(e["med_changes_count"], 5)
        x += 0.03 * (e["adi_decile"] - 5)
        # social drivers: only ever written in notes
        x += 0.45 * (h["lives_alone"] and not h["has_caregiver"]) + 0.50 * h["transport_barrier"]
        x += 0.45 * h["low_health_literacy"] + 0.70 * h["housing_unstable"]
        x += 0.50 * h["substance_use"] + 0.45 * h["med_nonadherence"]
        return float(x)

    def make_stay(self, n, p, h, admit, history, planned):
        age = h["age0"] + (admit - clock.WARMUP_START).days / 365.25
        dx = self.choose_dx(h, planned)
        if dx == "Heart failure":
            h["chf"] = True                       # a new diagnosis sticks to the patient
        if dx == "COPD exacerbation":
            h["copd"] = True
        if dx == "Acute kidney injury" and self.chance(0.3):
            h["ckd"] = True

        _, los_typ, icu_p = DX[dx]
        los = max(1, int(np.rint(los_typ * self.rng.lognormal(0, 0.45) * (1 + 0.12 * max(h["frailty"], 0)))))
        admit = admit.replace(hour=int(self.rng.integers(0, 24)), minute=int(self.rng.integers(0, 60)))
        discharge = (admit + timedelta(days=los)).replace(hour=int(self.rng.integers(10, 18)),
                                                          minute=int(self.rng.integers(0, 60)))
        w = {"Expired": 0.012 * (1 + max(h["frailty"], 0)) + 0.03 * (dx == "Sepsis"),
             "Hospice": 0.01 + 0.05 * h["cancer"] + 0.02 * h["dementia"],
             "Against medical advice": 0.015 + 0.08 * h["substance_use"] + 0.04 * h["housing_unstable"],
             "SNF": 0.04 + 0.10 * (age >= 75) + 0.10 * h["dementia"] + 0.05 * (los >= 7),
             "Home health": 0.12 + 0.08 * (age >= 70) + 0.06 * h["chf"]}
        w["Home"] = max(0.2, 1 - sum(w.values()))
        keys = list(w)
        pw = np.array([w[k] for k in keys])
        dispo = keys[int(self.rng.choice(len(keys), p=pw / pw.sum()))]

        year = timedelta(days=365)
        prior = [s for s in history if admit - year <= s["admit"] < admit]
        lam_ed = (0.25 + 0.35 * max(h["frailty"], 0) + 0.6 * h["substance_use"] + 0.5 * h["housing_unstable"]
                  + 0.2 * len(prior))

        def lab(mean, sd, miss=0.03):
            return float("nan") if self.chance(miss) else round(float(self.rng.normal(mean, sd)), 1)

        n_chronic = sum(h[k] for k in CHRONIC)
        creat = lab(1.0 + 1.1 * h["ckd"] + 0.8 * (dx == "Acute kidney injury"), 0.45)
        e = {
            "encounter_id": f"E{n:06d}", "patient_id": p["patient_id"],
            "admit_datetime": admit, "discharge_datetime": discharge, "planned_admission": planned,
            "age": int(age), "sex": p["sex"], "payer": p["payer"], "adi_decile": p["adi_decile"],
            "admission_type": "Elective" if planned else ("Emergency" if self.chance(0.82) else "Urgent"),
            "primary_dx_group": dx, "length_of_stay_days": los, "icu_stay": self.chance(icu_p),
            "discharge_disposition": dispo,
            **{k: bool(h[k]) for k in CHRONIC},
            "charlson_index": self.charlson(h, age),
            "prior_admissions_12m": len(prior),
            "ed_visits_6m": int(self.rng.poisson(lam_ed)),
            "prior_30d_readmits_12m": int(sum(s["was_readmission"] for s in prior)),
            "hemoglobin": lab(12.6 - 1.2 * h["ckd"] - 1.0 * h["cancer"] - 1.5 * (dx == "GI bleed"), 1.6),
            "sodium": lab(138.5 - 2.5 * h["chf"], 3.3),
            "creatinine": creat if math.isnan(creat) else round(max(0.4, creat), 2),
            # albumin is ordered for sicker patients: missing usually means "not ordered"
            "albumin": lab(3.6 - 0.3 * max(h["frailty"], 0) - 0.3 * (dx == "Sepsis"), 0.45,
                           miss=0.65 - 0.25 * (h["frailty"] > 0.5) - 0.2 * (dx == "Sepsis")),
            "discharge_sbp": int(self.rng.normal(128, 17)),
            "num_discharge_meds": int(self.rng.poisson(5 + 1.3 * n_chronic + 1.5 * max(h["frailty"], 0))),
            "high_risk_med": bool((h["diabetes"] and self.chance(0.45)) or (h["chf"] and self.chance(0.30))
                                  or self.chance(0.12)),
            "med_changes_count": int(self.rng.poisson(1.2 + 1.0 * (dx in ("Heart failure", "Diabetes complication")))),
            "followup_scheduled_7d": self.chance(0.62 + 0.12 * p["pcp_assigned"] - 0.20 * h["transport_barrier"]
                                                 - 0.15 * h["housing_unstable"]),
        }
        e["true_logit"] = self.risk_logit(e, h)
        e["true_risk"] = float(sigmoid(e["true_logit"]))
        return e

    # ------------------------------------------------------------------ timeline
    def patient_timeline(self, p, h, n_start):
        """All stays for one patient, in order. Returns (stays, next_encounter_number)."""
        span = (clock.LIVE_END - clock.WARMUP_START).days
        t = clock.WARMUP_START + timedelta(days=float(self.rng.uniform(0, span)))
        n, planned, history, prev_dis, stays = n_start, False, [], None, []
        while t <= clock.LIVE_END:
            n += 1
            e = self.make_stay(n, p, h, t, history, planned)
            if e["discharge_datetime"] > clock.LIVE_END:      # still in hospital when the simulation ends
                break
            gap_prev = (e["admit_datetime"] - prev_dis).days if prev_dis is not None else None
            was_readmit = (not planned) and gap_prev is not None and gap_prev <= clock.READMIT_WINDOW_DAYS
            history.append({"admit": e["admit_datetime"], "was_readmission": was_readmit})
            e["outcome_pending"] = e["admit_datetime"] >= clock.LIVE_START
            stays.append(e)
            prev_dis = e["discharge_datetime"]
            if e["discharge_disposition"] in ("Expired", "Hospice"):
                break
            if not e["outcome_pending"] and self.chance(e["true_risk"]):
                gap, planned = min(30, 1 + int(self.rng.gamma(1.4, 7))), False   # readmitted
            elif self.chance(0.05):
                gap, planned = int(self.rng.integers(7, 31)), True                 # scheduled procedure, doesn't count
            else:
                gap, planned = 31 + int(self.rng.exponential(420 * np.exp(-0.35 * h["frailty"]))), False
            t = (e["discharge_datetime"] + timedelta(days=gap)).replace(hour=0, minute=0)
        return stays, n

    # ------------------------------------------------------------------ build everything
    def build(self):
        patients, hidden, encs, notes, note_facts, enc_truth = [], [], [], [], [], []
        n = 0
        for i in range(1, self.n + 1):
            p, h = self.make_patient(i)
            stays, n = self.patient_timeline(p, h, n)
            exported = [s for s in stays if s["admit_datetime"] >= clock.EXPORT_START]
            if not exported:
                continue
            patients.append(p)
            hidden.append({**h, **{k: bool(h[k]) for k in CHRONIC}})
            for k, e in enumerate(stays):
                if e["admit_datetime"] < clock.EXPORT_START:
                    continue
                gap = next((round((s["admit_datetime"] - e["discharge_datetime"]).total_seconds() / 86400, 2)
                            for s in stays[k + 1:] if not s["planned_admission"]), None)
                for note, mentions in self.writer.write(e, h, p, gap, historical=not e["outcome_pending"]):
                    notes.append(note)
                    note_facts += [{"note_id": note["note_id"], "encounter_id": e["encounter_id"], "factor": f}
                                   for f in sorted(mentions)]
                enc_truth.append({"encounter_id": e["encounter_id"], "true_logit": round(e["true_logit"], 4),
                                  "true_risk": round(e["true_risk"], 4)})
                encs.append({k2: v for k2, v in e.items() if k2 not in ("true_logit", "true_risk")})

        enc = pd.DataFrame(encs)
        dups = enc.sample(frac=0.006, random_state=7)            # interface re-sends: real extracts have duplicates
        enc = pd.concat([enc, dups]).sort_values(["patient_id", "admit_datetime"]).reset_index(drop=True)
        return {
            "sim.patients": pd.DataFrame(patients),
            "sim.encounters": enc,
            "sim.notes": pd.DataFrame(notes),
            "truth.patients": pd.DataFrame(hidden),
            "truth.encounters": pd.DataFrame(enc_truth),
            "truth.note_facts": pd.DataFrame(note_facts, columns=["note_id", "encounter_id", "factor"]),
        }

