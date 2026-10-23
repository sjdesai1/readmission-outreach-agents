"""Does outreach reduce readmissions, and how sure can anyone be? Estimators graded against the simulation's truth.

    make effect                  # the 20x program (database `program`, run `make program` first)
    make effect db=app           # the demo program (46 cases): how noisy a small program is

Every flagged stay has two known chances of readmission (sim/outcomes.py): p0 with usual care, and p1 if the
patient attended a follow-up visit first. Which patients attended is fixed by the conversations that already
happened. Here only the outcome dice are re-rolled, many times: each draw is one possible version of what the
hospital would have observed. Each estimator is computed on every draw and compared with the truth.

Estimators (all use only what a hospital could see):
  randomized   readmission rate in the holdout minus in the outreach group. Unbiased over randomizations
  adjusted     the same, with the discharge risk score as a covariate (linear model, robust standard errors):
               corrects chance imbalance between the groups and narrows the interval
  naive        within outreach: patients who didn't attend a visit minus those who did. What a dashboard shows
               by default; biased, because the patients who show up differ from those who don't

Targets (the truth, known only in a simulation):
  program effect   mean over outreach patients of p0 minus their actual chance (p1 if they attended, else p0)
  attending effect mean over attenders of p0 minus p1

Reported per estimator: mean estimate, bias, spread across draws, CI width, coverage (how often the 95% CI
contains the truth: should be about 95%), and power (how often the CI excludes zero). Plus the sample size per
arm that would detect the true program effect.

Limits: the split into outreach and holdout is fixed (it was randomized once, by patient id), so the randomized
estimate inherits that split's baseline imbalance; it is reported. Visit timing is held as it happened.
"""

import argparse
import json
from statistics import NormalDist

import numpy as np

from readmit.config import REPO_ROOT

Z = NormalDist().inv_cdf(0.975)
MIN_PER_ARM = 10  # below this the intervals mean nothing


# ------------------------------------------------------------------ estimators (pure)
def diff_in_rates(y_a: np.ndarray, y_b: np.ndarray) -> tuple[float, float, float]:
    """Rate in a minus rate in b, with a 95% Wald interval."""
    pa, pb = y_a.mean(), y_b.mean()
    se = np.sqrt(pa * (1 - pa) / len(y_a) + pb * (1 - pb) / len(y_b))
    d = pa - pb
    return float(d), float(d - Z * se), float(d + Z * se)


def adjusted_effect(y: np.ndarray, treated_arm: np.ndarray, covariate: np.ndarray) -> tuple[float, float, float]:
    """Linear probability model y ~ 1 + arm + covariate. Returns the reduction (minus the arm coefficient) with a
    95% interval from heteroskedasticity-robust (HC1) standard errors."""
    X = np.column_stack([np.ones(len(y)), treated_arm.astype(float), covariate.astype(float)])
    beta, *_ = np.linalg.lstsq(X, y.astype(float), rcond=None)
    resid = y - X @ beta
    n, k = X.shape
    bread = np.linalg.inv(X.T @ X)
    meat = (X * resid[:, None] ** 2).T @ X
    cov = bread @ meat @ bread * n / (n - k)
    est, se = -beta[1], np.sqrt(cov[1, 1])
    return float(est), float(est - Z * se), float(est + Z * se)


def n_per_arm(p_control: float, effect: float, power: float = 0.8) -> int:
    """Patients per arm to detect `effect` (a drop in readmission rate) with a two-sided 5% test."""
    if effect <= 0:
        return 0
    p_treat = p_control - effect
    zb = NormalDist().inv_cdf(power)
    var = p_control * (1 - p_control) + p_treat * (1 - p_treat)
    return int(np.ceil((Z + zb) ** 2 * var / effect**2))


def grade(estimates: list[tuple[float, float, float]], truth: float) -> dict:
    e = np.array(estimates)
    return {
        "truth": truth,
        "mean_estimate": float(e[:, 0].mean()),
        "bias": float(e[:, 0].mean() - truth),
        "sd": float(e[:, 0].std(ddof=1)),
        "ci_width": float((e[:, 2] - e[:, 1]).mean()),
        "coverage": float(((e[:, 1] <= truth) & (truth <= e[:, 2])).mean()),
        "power": float((e[:, 1] > 0).mean()),
    }


def evaluate(holdout, p0, p1, attended, risk, draws: int = 1000, seed: int = 7) -> dict:
    """All arrays one entry per flagged stay. Re-rolls outcomes `draws` times and grades each estimator."""
    holdout, attended = np.asarray(holdout, bool), np.asarray(attended, bool)
    p0, p1, risk = (np.asarray(x, float) for x in (p0, p1, risk))
    p_actual = np.where(attended & ~holdout, p1, p0)  # holdout patients got no outreach, so no visit from it
    out = ~holdout
    truth_program = float((p0[out] - p_actual[out]).mean())
    att = out & attended
    truth_attending = float((p0[att] - p1[att]).mean()) if att.any() else 0.0
    imbalance = float(p0[holdout].mean() - p0[out].mean())  # baseline difference the split happened to create

    rng = np.random.default_rng(seed)
    rand, adj, naive = [], [], []
    for _ in range(draws):
        y = (rng.random(len(p0)) < p_actual).astype(float)
        rand.append(diff_in_rates(y[holdout], y[out]))
        adj.append(adjusted_effect(y, out, risk))
        if att.any() and (out & ~attended).any():
            naive.append(diff_in_rates(y[out & ~attended], y[att]))
    result = {
        "patients": {"outreach": int(out.sum()), "holdout": int(holdout.sum()), "attended": int(att.sum())},
        "truth": {"program_effect": truth_program, "attending_effect": truth_attending},
        "baseline_imbalance": imbalance,
        "randomized": grade(rand, truth_program),
        "adjusted": grade(adj, truth_program),
        "n_per_arm_for_80pct_power": n_per_arm(float(p0[out].mean()), truth_program),
    }
    if naive:
        result["naive"] = grade(naive, truth_attending)
    return result


# ------------------------------------------------------------------ data
def load() -> dict:
    from readmit import db

    with db.transaction() as c:
        if not db.one(c, "select to_regclass('truth.outcome_results') is not null as ok")["ok"]:
            raise SystemExit("no outcomes yet: run a simulation first (make simulate days=90, or make program)")
    rows = db.read(
        """select o.holdout, o.p0, o.p1, o.treated, s.risk
           from truth.outcome_results o join scoring.scores s using (encounter_id)"""
    )
    if rows.empty:
        raise SystemExit("no decided outcomes yet: run the simulation long enough for 30-day outcomes")
    return {
        "holdout": rows["holdout"].to_numpy(),
        "p0": rows["p0"].to_numpy(),
        "p1": rows["p1"].to_numpy(),
        "attended": rows["treated"].to_numpy(),
        "risk": rows["risk"].to_numpy(),
    }


def main() -> None:
    from readmit.config import settings

    ap = argparse.ArgumentParser()
    ap.add_argument("--draws", type=int, default=1000)
    args = ap.parse_args()
    data = load()
    n_hold, n_out = int(data["holdout"].sum()), int((~data["holdout"].astype(bool)).sum())
    if min(n_hold, n_out) < MIN_PER_ARM:
        raise SystemExit(
            f"only {n_out} outreach and {n_hold} holdout outcomes decided in database {settings.db_name}: need "
            f"{MIN_PER_ARM}+ per arm. Run the simulation long enough for 30-day outcomes, e.g. "
            "make outreach-setup && make simulate days=120"
        )
    r = evaluate(**data, draws=args.draws)

    p, t = r["patients"], r["truth"]
    print(
        f"\n  database {settings.db_name}: {p['outreach']} outreach, {p['holdout']} holdout, {p['attended']} attended"
    )
    print(f"  truth: program effect {t['program_effect']:.1%} points, attending effect {t['attending_effect']:.1%}")
    print(
        f"  baseline imbalance from the split (holdout minus outreach, usual-care risk): {r['baseline_imbalance']:+.1%}"
    )
    print(f"\n  {r['patients']['outreach'] + r['patients']['holdout']} patients, {args.draws} outcome draws")
    print(f"  {'estimator':<11} {'target':<10} {'truth':>6} {'mean est':>9} {'bias':>7} {'sd':>6} {'CI width':>9} "
          f"{'coverage':>9} {'power':>6}")  # fmt: skip
    for name, target in (("randomized", "program"), ("adjusted", "program"), ("naive", "attending")):
        if name not in r:
            continue
        g = r[name]
        print(
            f"  {name:<11} {target:<10} {g['truth']:>6.1%} {g['mean_estimate']:>9.1%} {g['bias']:>+7.1%} "
            f"{g['sd']:>6.1%} {g['ci_width']:>9.1%} {g['coverage']:>9.0%} {g['power']:>6.0%}"
        )
    print(f"\n  patients per arm to detect the true program effect with 80% power: {r['n_per_arm_for_80pct_power']:,}")

    out = REPO_ROOT / "data" / "reports"
    out.mkdir(parents=True, exist_ok=True)
    (out / f"effect_{settings.db_name}.json").write_text(json.dumps(r, indent=2))
    print(f"  saved data/reports/effect_{settings.db_name}.json")

    import mlflow

    mlflow.set_tracking_uri(settings.mlflow_uri)
    mlflow.set_experiment("program-effect")
    with mlflow.start_run(run_name=f"effect {settings.db_name}"):
        mlflow.log_params({"database": settings.db_name, "draws": args.draws, **r["patients"]})
        mlflow.log_metrics({"truth_program": t["program_effect"], "imbalance": r["baseline_imbalance"]})
        for name in ("randomized", "adjusted", "naive"):
            if name in r:
                mlflow.log_metrics({f"{name}_{k}": v for k, v in r[name].items()})


if __name__ == "__main__":
    main()
