"""Messy clinical notes.

Written the way real notes look: abbreviations, inline ICD-10 codes, negations, rule-outs,
family history, copy-forward, typos, late-signed summaries. Social factors appear ONLY here,
never in structured columns, so the LLM extractor has real work to do.

Every positive social mention is recorded per note (`mentions`), so the extractor can later be
scored against exactly what was written before the prediction cutoff.
"""

from datetime import timedelta

SOCIAL_FACTORS = [
    "lives_alone",
    "no_caregiver",
    "transport_barrier",
    "low_health_literacy",
    "housing_unstable",
    "substance_use",
    "med_nonadherence",
]

PMH = {  # how each chronic condition gets written; codes appear inline sometimes
    "chf": ["CHF", "HFrEF (EF {ef}%)", "HFpEF", "sys HF, EF {ef}%", "I50.22 chronic systolic HF"],
    "copd": ["COPD", "COPD on 2L home O2", "J44.9 COPD", "chronic obstructive pulm dz"],
    "diabetes": ["DM2", "T2DM on insulin", "E11.65 DM2 w/ hyperglycemia", "type 2 diabetes"],
    "ckd": ["CKD3b", "CKD stage 4", "N18.32 CKD", "chronic kidney dz"],
    "cancer": ["hx breast ca s/p mastectomy", "prostate ca", "C34.90 lung ca", "metastatic colon ca"],
    "depression": ["MDD", "depression on sertraline", "F32.9", "anxiety/depression"],
    "dementia": ["dementia", "Alzheimer's dz", "F03.90 dementia", "mild cognitive impairment"],
}
PMH_NEGATION = {
    "chf": "Denies hx of CHF.",
    "copd": "No h/o COPD.",
    "diabetes": "No hx DM.",
    "ckd": "No known CKD.",
    "cancer": "No hx of malignancy.",
}

PRESENT = {
    "Heart failure": ["p/w SOB, DOE x3d, BLE edema", "p/w orthopnea, 4kg wt gain, +JVD"],
    "COPD exacerbation": ["p/w incr cough, wheeze, sputum x2d", "p/w acute SOB not relieved by home neb"],
    "Pneumonia": [
        "p/w fever, prod cough, RLL infiltrate on CXR",
        "p/w cough/fevers, hypoxic to 88% RA, LLL consolidation",
    ],
    "Sepsis": ["p/w fever, hypotension, AMS; lactate 4.1", "p/w rigors, HR 120s, BP 80s/50s"],
    "Diabetes complication": ["p/w BG >500, polyuria", "p/w DKA, AG 24"],
    "Acute MI": ["p/w substernal CP, trop elevated", "p/w chest pressure radiating to L arm, NSTEMI"],
    "Acute kidney injury": ["p/w Cr 3.1 from baseline 1.2, poor PO intake", "p/w decr UOP, AKI"],
    "GI bleed": ["p/w melena, Hgb drop 11->7.8", "p/w coffee-ground emesis, lightheaded"],
    "Cellulitis": ["p/w spreading erythema LLE", "p/w painful swelling/redness R leg"],
    "Other": ["p/w generalized weakness", "admitted for scheduled procedure", "p/w abd pain"],
}

SOCIAL_POS = {  # how each factor is written when it IS present for the patient
    "lives_alone": ["Lives alone in apt.", "Pt lives by self, dtr out of state.", "Lives alone in walkup apt."],
    "no_caregiver": ["No caregiver at home.", "No family/friends nearby to help."],
    "transport_barrier": [
        "No car, relies on bus.",
        "Has missed prior appts d/t no ride.",
        "Can't afford cab to appts.",
    ],
    "low_health_literacy": [
        "Teach-back incomplete, unable to restate new med schedule.",
        "Pt confused re: d/c instructions despite repeat teaching.",
        "Limited understanding of dx, needed instructions repeated several times.",
    ],
    "housing_unstable": [
        "Homeless, staying at shelter.",
        "Recently evicted, couch surfing.",
        "Sleeping in car intermittently.",
    ],
    "substance_use": ["ETOH 6-8 beers/day.", "+meth use per pt.", "Daily ETOH, last drink AM of admit."],
    "med_nonadherence": [
        "Ran out of home meds ~1wk PTA.",
        "Not taking lasix as rx'd d/t cost.",
        "Often skips doses, pillbox not used.",
    ],
}
SOCIAL_NEG = {  # how the ABSENCE is written; the extractor must not count these
    "lives_alone": ["Lives w/ wife.", "Lives w/ dtr and son-in-law.", "Lives w/ supportive partner."],
    "transport_barrier": ["Son drives to appts.", "Has own car."],
    "low_health_literacy": ["Able to teach back med changes.", "Verbalized understanding of d/c plan."],
    "housing_unstable": ["Stable housing.", "Owns home."],
    "substance_use": ["Denies ETOH/drugs.", "No tobacco, ETOH or illicit drug use."],
    "med_nonadherence": ["Takes meds as rx'd, uses pillbox.", "Family helps fill weekly pillbox."],
}
TRAPS = [  # mention a factor's words about someone else: must NOT count as the patient's
    "Dtr lives alone nearby and checks in.",
    "Pt's husband has hx of ETOH use, now sober.",
    "Brother recently evicted, staying with pt.",
]
FHX_MOTHER = ["mother w/ DM2", "mother w/ breast ca", "mother w/ HTN"]
FHX_FATHER = ["father w/ MI", "father w/ COPD", "father w/ ETOH use d/o"]


def _flags(h):
    return {
        "lives_alone": h["lives_alone"],
        "no_caregiver": h["lives_alone"] and not h["has_caregiver"],
        "transport_barrier": h["transport_barrier"],
        "low_health_literacy": h["low_health_literacy"],
        "housing_unstable": h["housing_unstable"],
        "substance_use": h["substance_use"],
        "med_nonadherence": h["med_nonadherence"],
    }


class NoteWriter:
    def __init__(self, rng):
        self.rng = rng

    def chance(self, p):
        return bool(self.rng.random() < p)

    def pick(self, options):
        return options[int(self.rng.integers(len(options)))]

    def typos(self, text, p_note=0.3, p_word=0.03):
        """Swap two letters in a few longer words: real notes are full of these."""
        if not self.chance(p_note):
            return text
        words = text.split(" ")
        for i, w in enumerate(words):
            if len(w) > 5 and w.isalpha() and self.chance(p_word):
                j = int(self.rng.integers(1, len(w) - 2))
                words[i] = w[:j] + w[j + 1] + w[j] + w[j + 2 :]
        return " ".join(words)

    def social(self, h, p_pos, p_neg, mentions):
        """Sentences about the patient's social situation. Records each positive mention."""
        out = []
        for k, present in _flags(h).items():
            if present and self.chance(p_pos):
                out.append(self.pick(SOCIAL_POS[k]))
                mentions.add(k)
            elif not present and k in SOCIAL_NEG and self.chance(p_neg):
                out.append(self.pick(SOCIAL_NEG[k]))
        if self.chance(0.08):
            out.append(self.pick(TRAPS))
        self.rng.shuffle(out)
        return " ".join(out)

    def write(self, e, h, p, next_unplanned_gap_days, historical):
        """All notes for one stay. Returns list of (note_dict, set_of_mentions)."""
        notes = []
        adm, dis, dx = e["admit_datetime"], e["discharge_datetime"], e["primary_dx_group"]
        who = "F" if p["sex"] == "F" else "M"
        ef = int(self.rng.integers(20, 45))

        def add(kind, when, text, author, mentions):
            text = self.typos(text)
            notes.append(
                (
                    {
                        "note_id": f"{e['encounter_id']}-N{len(notes) + 1}",
                        "encounter_id": e["encounter_id"],
                        "patient_id": e["patient_id"],
                        "note_type": kind,
                        "author_role": author,
                        "note_datetime": when,
                        "text": text,
                    },
                    mentions,
                )
            )

        # --- H&P -------------------------------------------------------------
        m = set()
        pmh = [self.pick(PMH[k]).format(ef=ef) for k in PMH if e[k]]
        negs = [PMH_NEGATION[k] for k in PMH_NEGATION if not e[k] and self.chance(0.12)]
        nonadh = ""
        if h["med_nonadherence"] and self.chance(0.4):
            nonadh = " " + self.pick(SOCIAL_POS["med_nonadherence"])
            m.add("med_nonadherence")
        ro = " r/o PNA, CXR neg." if dx != "Pneumonia" and self.chance(0.15) else ""
        interp = (
            " Spanish-speaking, interpreter used." if p["preferred_language"] == "Spanish" and self.chance(0.7) else ""
        )
        fhx = f"\nFHx: {self.pick(FHX_MOTHER)}, {self.pick(FHX_FATHER)}." if self.chance(0.5) else ""
        sh = self.social(h, 0.35, 0.30, m)
        add(
            "H&P",
            adm + timedelta(hours=float(self.rng.uniform(2, 8))),
            f"H&P\nCC: {dx.lower()}\nHPI: {e['age']}{who} w/ PMH {', '.join(pmh) or 'none'} {self.pick(PRESENT[dx])}."
            f"{nonadh}{ro}{interp}\n{' '.join(negs)}{fhx}\nSH: {sh or 'not obtained on admit.'}\n"
            f"Home meds: {max(e['num_discharge_meds'] - e['med_changes_count'], 0)} reviewed.\n"
            f"A/P: admit to hospital medicine for {dx.lower()}." + (" Transfer to ICU." if e["icu_stay"] else ""),
            "Physician",
            m,
        )
        copied = sh.split(". ")[0].rstrip(".") + "." if sh and self.chance(0.3) else ""  # copy-forward

        # --- progress notes ----------------------------------------------------
        for d in range(1, min(e["length_of_stay_days"], 4)):
            m = set()
            if copied:  # a copied social line is still a mention, just a stale one
                for k, phrases in SOCIAL_POS.items():
                    if any(copied.rstrip(".") in ph for ph in phrases):
                        m.add(k)
            add(
                "Progress",
                adm + timedelta(days=d, hours=float(self.rng.uniform(7, 11))),
                f"PROGRESS HD#{d + 1}\nS: {self.pick(['feels somewhat better', 'NAEON', 'still SOB w/ exertion', 'tolerating PO'])}.\n"
                f"P: cont mgmt {dx.lower()}; {self.pick(['trend labs', 'PT/OT eval', 'wean O2 as tol', 'adjust diuresis'])}. {copied}",
                "Physician",
                m,
            )

        # --- case management ----------------------------------------------------
        if self.chance(0.55 + 0.25 * (e["discharge_disposition"] != "Home") + 0.15 * h["housing_unstable"]):
            m = set()
            cm = self.social(h, 0.85, 0.55, m)
            add(
                "Case management",
                dis - timedelta(hours=float(self.rng.uniform(3, 20))),
                f"CM NOTE\nMet w/ pt{' and family' if h['has_caregiver'] and self.chance(0.5) else ''} re: d/c planning.\n"
                f"Living situation/supports: {cm or 'no barriers identified.'}\nIns: {p['payer']}.\n"
                f"D/c plan: {e['discharge_disposition'].lower()}.",
                "Case manager",
                m,
            )

        # --- discharge summary (sometimes signed late: after the prediction cutoff) ----
        m = set()
        teach = ""
        if h["low_health_literacy"] and self.chance(0.45):
            teach = " " + self.pick(SOCIAL_POS["low_health_literacy"])
            m.add("low_health_literacy")
        fu = (
            "PCP f/u scheduled within 7d."
            if e["followup_scheduled_7d"]
            else "Pt to call PCP to schedule f/u in 1-2 wks."
        )
        signed = self.rng.uniform(-3, 20) if self.chance(0.8) else self.rng.uniform(26, 72)
        add(
            "Discharge summary",
            dis + timedelta(hours=float(signed)),
            f"D/C SUMMARY\nPrincipal dx: {dx}.\nHosp course: {e['length_of_stay_days']}d stay"
            f"{' incl ICU' if e['icu_stay'] else ''}, treated for {dx.lower()}.\n"
            f"D/c meds: {e['num_discharge_meds']} total, {e['med_changes_count']} changed"
            f"{', incl high-risk med' if e['high_risk_med'] else ''}.\nDispo: {e['discharge_disposition']}.{teach}\nF/u: {fu}",
            "Physician",
            m,
        )

        # --- nursing discharge ------------------------------------------------------
        if self.chance(0.5):
            m = set()
            nurse = self.social(h, 0.30, 0.25, m)
            ride = (
                "Picked up by family."
                if h["has_caregiver"]
                else self.pick(["Left by taxi.", "Left by bus.", "Left alone."])
            )
            add(
                "Nursing discharge",
                dis - timedelta(minutes=float(self.rng.uniform(10, 90))),
                f"RN D/C NOTE\nD/c instructions reviewed. {nurse} {ride}",
                "Nurse",
                m,
            )

        # --- post-discharge call: written 2-3 days AFTER discharge, leaks the outcome ---
        # Only for history: in the live period the outcome isn't decided yet.
        if historical and e["discharge_disposition"] not in ("Expired", "Hospice") and self.chance(0.6):
            gap = next_unplanned_gap_days
            if gap is not None and gap <= 3:
                txt = "Spoke w/ family, pt went back to ED and was readmitted."
            elif gap is not None and gap <= 30:
                txt = self.pick(
                    ["Pt reports worsening sx since d/c, advised ED.", "Pt unable to fill meds, feels worse."]
                )
            else:
                txt = self.pick(["Pt doing well, has meds and f/u.", "Unable to reach pt, LVM."])
            add(
                "Post-discharge call",
                dis + timedelta(hours=float(self.rng.uniform(48, 72))),
                f"POST-D/C CALL\n{txt}",
                "Nurse",
                set(),
            )
        return notes
