"""
Equation adoption review service.

Goal
====
When a research team proposes a *new* allometric equation set, any difference
it produces against a published estimate must be attributable to THE EQUATION
ALONE — never to changed survey data, changed human identity decisions, or
sampling-frame drift. This service therefore:

1. keeps the candidate as an immutable JSON spec (it is NOT an
   AllometricEquation row until approved, so the ordinary estimate flow can
   never select it);
2. for an impact comparison, LOCKS one already-confirmed EstimateVersion by
   fingerprinting its measurements, identity decisions and frame, and by
   re-running the baseline equations and demanding a byte-identical result;
3. runs the candidate over exactly the same locked inputs and reports
   tree-level, plot-level and population-level differences, extrapolations
   and missing coverage (a coverage matrix);
4. approves only a *complete* comparison against an unchanged lock, creating
   NEW AllometricEquation rows and a NEW confirmed EstimateVersion. The
   baseline version, its equations and its result are never modified.

Lifecycle: candidate -> validated -> approved | withdrawn.
"""
import hashlib
import json
import math
from collections import defaultdict

from django.db import transaction
from django.utils import timezone

from inventory.models import (
    AllometricEquation,
    COMPARISON_COMPLETE,
    COMPARISON_INCOMPLETE,
    EquationAdoptionReview,
    EstimateVersion,
    REVIEW_APPROVED,
    REVIEW_CANDIDATE,
    REVIEW_VALIDATED,
    REVIEW_WITHDRAWN,
    ReviewComparison,
    ReviewEvent,
    Species,
    STATUS_ALIVE_MEASURED,
    STATUS_ALIVE_NOT_MEASURED,
    STATUS_DEAD,
    VERSION_CONFIRMED,
)
from inventory.services.estimator import (
    biomass_kg,
    build_measurement_table,
    estimate,
    equation_checksum,
    resolved_identity_pairs,
)


# ---------------------------------------------------------------- utilities
def _canonical(obj):
    """Deterministic JSON text for fingerprinting (numpy scalars tolerated)."""
    import numpy as np

    def coerce(v):
        if isinstance(v, (np.floating,)):
            return float(v)
        if isinstance(v, (np.integer,)):
            return int(v)
        if isinstance(v, (np.ndarray,)):
            return v.tolist()
        raise TypeError(f"cannot canonicalise {type(v)!r}")

    return json.dumps(obj, sort_keys=True, default=coerce,
                      ensure_ascii=False)


def canonical_hash(obj):
    return hashlib.sha256(_canonical(obj).encode("utf-8")).hexdigest()


def spec_map_from_payload(candidate_spec):
    """candidate_spec is {species_code: equation_fields}."""
    out = {}
    for sp, e in candidate_spec.items():
        out[sp] = {
            "code": e["code"], "version": e["version"],
            "a": float(e["a"]), "b": float(e["b"]), "c": float(e["c"]),
            "dbh_min_cm": float(e["dbh_min_cm"]),
            "dbh_max_cm": float(e["dbh_max_cm"]),
            "height_required": bool(e.get("height_required", True)),
            "residual_sigma": float(e["residual_sigma"]),
            "citation": e["citation"],
            "form": e.get("form",
                          "agb = a * dbh_cm^b * height_m^c"),
        }
    return out


def _agb(row, eq):
    if eq is None or row.get("dbh_cm") is None:
        return None
    if eq.get("height_required") and eq["c"] != 0.0 and row.get("height_m") is None:
        return None
    return float(biomass_kg([row["dbh_cm"]], [row["height_m"]], eq)[0])


def _in_range(dbh, eq):
    return eq["dbh_min_cm"] <= dbh <= eq["dbh_max_cm"]


# --------------------------------------------------------------- fingerprints
def current_fingerprints(t1, t2):
    """
    Hash every equation-independent input that feeds an estimate:

    * measurements (canonical values AND raw entries/units/notes),
    * identity decisions (conflict resolutions + verified renumber links),
    * sampling frame (plots/boundaries/areas + stratum land areas + campaigns).
    """
    from inventory.models import (
        IdentityConflict, Plot, Stratum, Tree, TreeMeasurement,
    )

    meas = sorted(
        TreeMeasurement.objects
        .filter(campaign__in=[t1, t2])
        .values_list("id", "tree_id", "campaign_id", "field_number_seen",
                     "x_m", "y_m", "status",
                     "dbh_raw", "dbh_unit", "dbh_cm",
                     "height_raw", "height_unit", "height_m", "notes"),
    )
    conflicts = sorted(
        IdentityConflict.objects
        .filter(t1_campaign=t1, t2_campaign=t2)
        .values_list("id", "t1_measurement_id", "t2_measurement_id",
                     "distance_m", "status", "resolution_note"),
    )
    trees = sorted(
        Tree.objects.values_list("id", "plot_id", "current_field_number",
                                 "species_id", "superseded_tree_id",
                                 "first_campaign_id"),
    )
    plots = sorted(
        Plot.objects.values_list("id", "code", "stratum_id", "x_m", "y_m",
                                 "declared_area_ha", "area_polygon_ha",
                                 "boundary"),
    )
    strata = sorted(
        Stratum.objects.values_list("id", "code", "name", "area_ha"),
    )
    return {
        "measurements": canonical_hash(meas),
        "identity_conflicts": canonical_hash(conflicts),
        "tree_identity": canonical_hash(trees),
        "plots": canonical_hash(plots),
        "strata": canonical_hash(strata),
        "campaigns": canonical_hash([
            (t1.code, str(t1.measured_on)),
            (t2.code, str(t2.measured_on)),
        ]),
    }


# ----------------------------------------------------------- run / reproduction
def _design_from_version(version):
    snap = version.design_snapshot
    keys = ("t1_code", "t2_code", "interval_years", "dbh_sd_cm",
            "height_sd_m", "zero_tol_cm", "recruitment_cm", "fpc",
            "crs_epsg")
    return {k: snap[k] for k in keys}


def _uncovered_species(table, equations):
    return sorted({r["species"] for r in table if r["species"] not in equations})


def run_version_inputs(version, equations):
    """
    Run the estimator over version's locked campaigns with the supplied
    species->equation map. ``equations`` may be a queryset (re-reads the
    baseline rows) or a ready dict (candidate map, no DB rows needed).
    """
    t1, t2 = version.t1_campaign, version.t2_campaign
    if isinstance(equations, dict):
        # tables/plots/strata must still be built; use baseline equations for
        # the table builder (its equation map is discarded/replaced below).
        t1t, t2t, _base, plots, strata = build_measurement_table(
            t1, t2, version.equations.all())
        eq_map = equations
    else:
        t1t, t2t, eq_map, plots, strata = build_measurement_table(
            t1, t2, equations)
    renumber, distinct = resolved_identity_pairs(t1, t2)
    design = _design_from_version(version)
    result = estimate(t1t, t2t, eq_map, plots, strata, design,
                      resolved_renumber_pairs=renumber,
                      resolved_distinct_pairs=distinct)
    result["species_without_equation"] = _uncovered_species(
        t1t + t2t, eq_map)
    return {
        "result": result, "table_t1": t1t, "table_t2": t2t,
        "equations": eq_map, "plots": plots, "strata": strata,
        "renumber": renumber, "distinct": distinct,
    }


def baseline_equations_map(version):
    eqs = {}
    for e in version.equations.all().prefetch_related("species"):
        for sp in e.species.all():
            eqs[sp.code] = {
                "code": e.code, "version": e.version,
                "a": e.a, "b": e.b, "c": e.c,
                "dbh_min_cm": e.dbh_min_cm, "dbh_max_cm": e.dbh_max_cm,
                "height_required": e.height_required,
                "residual_sigma": e.residual_sigma, "citation": e.citation,
                "form": e.form,
            }
    return eqs


# ------------------------------------------------------------ eligible tree rows
def _eligible_rows(run, pairing):
    """
    Measurement rows that actually receive a biomass value under the
    estimator's component logic, tagged with the component they feed.
    Mirrors compute_plot_components' branches exactly.
    """
    out = []

    def add(row, occasion, component):
        out.append({"row": row, "occasion": occasion, "component": component})

    for pair in pairing["pairs"]:
        r1, r2 = pair["t1"], pair["t2"]
        if r2["status"] == STATUS_DEAD:
            if r1["status"] == STATUS_ALIVE_MEASURED:
                add(r1, "t1", "mortality")
        elif r2["status"] == STATUS_ALIVE_MEASURED:
            if r1["status"] == STATUS_ALIVE_MEASURED:
                add(r1, "t1", "survivor")
                add(r2, "t2", "survivor")
        elif r2["status"] == STATUS_ALIVE_NOT_MEASURED:
            if r1["status"] == STATUS_ALIVE_MEASURED:
                add(r1, "t1", "survivor_imputed")

    for r1 in pairing["t1_only"]:
        if r1["status"] == STATUS_ALIVE_MEASURED:
            add(r1, "t1", "mortality_distinct")
    for r2 in pairing["t2_only"]:
        if r2["status"] == STATUS_ALIVE_MEASURED and r2["dbh_cm"] is not None:
            if r2["dbh_cm"] >= run["result"]["design"]["recruitment_dbh_cm"]:
                add(r2, "t2", "ingrowth")
            # below-recruitment stems never receive equation biomass
    return out


# ------------------------------------------------------------- coverage matrix
DBH_CLASS_BOUNDS = [5.0, 10.0, 20.0, 40.0, 60.0, 80.0, 100.0]


def _dbh_class(dbh):
    lo = 5.0
    for hi in DBH_CLASS_BOUNDS[1:]:
        if dbh < hi:
            return f"{lo:g}–{hi:g}"
        lo = hi
    return f"{lo:g}+"


def build_coverage_and_tree_diff(run_base, run_cand, pairing):
    base_eqs = run_base["equations"]
    cand_eqs = run_cand["equations"]
    eligible = _eligible_rows(run_base, pairing)

    # species present in ANY eligible numeric row
    species_seen = sorted({e["row"]["species"] for e in eligible})
    by_species = {s: [] for s in species_seen}
    tree_rows = []

    for e in eligible:
        row, occ, comp = e["row"], e["occasion"], e["component"]
        sp = row["species"]
        b_eq, c_eq = base_eqs.get(sp), cand_eqs.get(sp)
        b_agb = _agb(row, b_eq)
        c_agb = _agb(row, c_eq)
        out_of_range = (
            c_eq is not None and row["dbh_cm"] is not None
            and not _in_range(row["dbh_cm"], c_eq)
        )
        rec = {
            "tree": f"{row['plot']}/{row['field_number']}",
            "tree_id": row["tree_id"],
            "plot": row["plot"],
            "species": sp,
            "occasion": occ,
            "component": comp,
            "dbh_cm": row["dbh_cm"],
            "baseline_equation": (f"{b_eq['code']}@{b_eq['version']}"
                                  if b_eq else None),
            "candidate_equation": (f"{c_eq['code']}@{c_eq['version']}"
                                   if c_eq else None),
            "baseline_agb_kg": (round(b_agb, 3) if b_agb is not None else None),
            "candidate_agb_kg": (round(c_agb, 3) if c_agb is not None else None),
            "delta_agb_kg": (round(c_agb - b_agb, 3)
                             if b_agb is not None and c_agb is not None else None),
            "delta_percent": (round(100.0 * (c_agb - b_agb) / b_agb, 2)
                              if b_agb not in (None, 0) and c_agb is not None
                              else None),
            "candidate_missing_species": c_eq is None,
            "candidate_out_of_range": out_of_range,
            "candidate_valid_range_cm": (
                [c_eq["dbh_min_cm"], c_eq["dbh_max_cm"]] if c_eq else None),
        }
        tree_rows.append(rec)
        by_species[sp].append(rec)

    tree_rows.sort(key=lambda r: (r["plot"], r["tree"], r["occasion"]))

    matrix, missing_species, out_of_range_trees = [], [], []
    for sp in species_seen:
        recs = by_species[sp]
        dbhs = [r["dbh_cm"] for r in recs if r["dbh_cm"] is not None]
        c_eq = cand_eqs.get(sp)
        b_eq = base_eqs.get(sp)
        missing = c_eq is None
        oor = [r for r in recs if r["candidate_out_of_range"]]
        if missing:
            missing_species.append(sp)
        out_of_range_trees.extend(
            {"species": sp, "tree": r["tree"], "dbh_cm": r["dbh_cm"],
             "candidate_range_cm": r["candidate_valid_range_cm"]}
            for r in oor)

        classes = []
        for lo, hi in zip(DBH_CLASS_BOUNDS, DBH_CLASS_BOUNDS[1:] + [None]):
            in_bin = [r for r in recs
                      if r["dbh_cm"] is not None
                      and r["dbh_cm"] >= lo
                      and (hi is None or r["dbh_cm"] < hi)]
            if not in_bin:
                continue
            bin_oor = [r for r in in_bin if r["candidate_out_of_range"]]
            classes.append({
                "dbh_class_cm": (f"{lo:g}–{hi:g}" if hi is not None
                                 else f"{lo:g}+"),
                "n_measurements": len(in_bin),
                "n_trees": len({r["tree"] for r in in_bin}),
                "candidate_covers_class": c_eq is not None
                                          and not bin_oor,
                "out_of_range_trees": [r["tree"] for r in bin_oor],
            })

        matrix.append({
            "species": sp,
            "baseline_equation": (f"{b_eq['code']}@{b_eq['version']}"
                                  if b_eq else None),
            "candidate_equation": (f"{c_eq['code']}@{c_eq['version']}"
                                   if c_eq else None),
            "n_trees": len({r["tree"] for r in recs}),
            "n_measurements": len(recs),
            "observed_dbh_range_cm": [round(min(dbhs), 2),
                                      round(max(dbhs), 2)] if dbhs else None,
            "candidate_dbh_range_cm": (
                [c_eq["dbh_min_cm"], c_eq["dbh_max_cm"]] if c_eq else None),
            "covered": (not missing) and not oor,
            "missing_species": missing,
            "out_of_range_count": len(oor),
            "out_of_range_trees": [r["tree"] for r in oor],
            "dbh_classes": classes,
        })

    return {
        "matrix": matrix,
        "tree_differences": tree_rows,
        "missing_species": missing_species,
        "out_of_range_trees": out_of_range_trees,
    }


# ----------------------------------------------------------------- comparison
def _component_diff(base, cand, keys=("total_mg",)):
    out = {}
    for k in keys:
        bv, cv = base[k], cand[k]
        out[k] = {
            "baseline": bv, "candidate": cv,
            "delta": cv - bv,
            "delta_percent": (100.0 * (cv - bv) / bv if bv else None),
        }
    return out


def _population_diff(rb, rc):
    comps = {}
    for name in ("survivor_growth", "mortality", "ingrowth"):
        comps[name] = _component_diff(
            rb["components"][name], rc["components"][name],
            keys=("total_mg", "se_total_kg", "se_sampling_kg",
                  "se_equation_residual_kg"))
    net = _component_diff(rb["net_change"], rc["net_change"],
                          keys=("total_mg", "se_total_kg"))
    stocks = {
        "t1": _component_diff(rb["stocks"], rc["stocks"],
                              keys=("t1_mg",)),
        "t2": _component_diff(rb["stocks"], rc["stocks"],
                              keys=("t2_mg",)),
    }
    return {"components": comps, "net_change": net, "stocks": stocks}


def _plot_diff(rb, rc):
    bp = {p["plot"]: p for p in rb["provenance"]["plots"]}
    cp = {p["plot"]: p for p in rc["provenance"]["plots"]}
    rows = []
    for code in sorted(bp):
        bkg, ckg = bp[code]["kg"], cp[code]["kg"]
        rows.append({
            "plot": code, "stratum": bp[code]["stratum"],
            "area_ha": bp[code]["area_ha"],
            "kg": {
                k: {"baseline": bkg[k], "candidate": ckg[k],
                    "delta": round(ckg[k] - bkg[k], 3)}
                for k in ("survivor_growth", "mortality", "ingrowth",
                          "stock_t1", "stock_t2")
            },
            "baseline_extrapolations": [
                e["tree"] for e in bp[code]["equation_range_extrapolations"]],
            "candidate_extrapolations": [
                e["tree"] for e in cp[code]["equation_range_extrapolations"]],
            "candidate_missing_inputs": [
                e["tree"] for e in cp[code]["equation_missing_inputs"]],
        })
    return rows


def create_comparison(review, version, actor="", note=""):
    """
    Lock ``version`` (must be confirmed) and compare the candidate against it.

    Raises ReviewLockError if the baseline does not reproduce (data/identity/
    frame drift): no comparison is stored, but the failed attempt is audited.
    """
    if review.status not in (REVIEW_VALIDATED, REVIEW_APPROVED):
        raise ReviewStateError(
            "candidate must be validated before an impact comparison")
    if version.status != VERSION_CONFIRMED:
        raise ReviewStateError(
            "baseline EstimateVersion must be confirmed/frozen before it is "
            "locked for comparison")

    t1, t2 = version.t1_campaign, version.t2_campaign
    fingerprints = current_fingerprints(t1, t2)

    # 1) re-run the baseline equations on the CURRENT data
    run_base = run_version_inputs(version, version.equations.all())
    base_hash = canonical_hash(run_base["result"])
    stored_hash = canonical_hash(version.result_payload)
    reproduced = base_hash == stored_hash

    # 2) frame snapshot embedded at confirmation must still describe reality
    frame_drift = _frame_snapshot_drift(version, run_base["strata"],
                                        run_base["plots"])

    if not reproduced or frame_drift:
        detail = {
            "refused": True,
            "baseline_reproduced": reproduced,
            "frame_snapshot_drift": frame_drift,
            "fingerprints": fingerprints,
            "baseline_result_hash": base_hash,
            "stored_result_hash": stored_hash,
        }
        ReviewEvent.objects.create(
            review=review, event=ReviewEvent.EVENT_COMPARED, actor=actor,
            note="comparison refused: " + (
                "confirmed result not reproducible (data/identity/frame "
                "drift)" if not reproduced else
                "sampling-frame snapshot drift"),
            payload=detail)
        raise ReviewLockError(detail)

    # 3) run the candidate over the IDENTICAL locked inputs. The candidate map
    # contains ONLY the proposed species equations — no silent baseline
    # fallback — so an omitted species is numerically uncovered.
    cand_map = spec_map_from_payload(review.candidate_spec)
    run_cand = run_version_inputs(version, cand_map)

    from inventory.services.identity import pair_measurements
    pairing = pair_measurements(
        run_base["table_t1"], run_base["table_t2"],
        resolved_renumber_pairs=run_base["renumber"],
        resolved_distinct_pairs=run_base["distinct"])

    coverage = build_coverage_and_tree_diff(run_base, run_cand, pairing)

    incomplete_reasons = []
    if coverage["missing_species"]:
        incomplete_reasons.append({
            "kind": "missing_species",
            "species": coverage["missing_species"],
            "detail": "candidate provides no equation for species covered "
                      "by the locked baseline",
        })
    if coverage["out_of_range_trees"]:
        incomplete_reasons.append({
            "kind": "out_of_dbh_range",
            "trees": coverage["out_of_range_trees"],
            "detail": "eligible trees lie outside the candidate's dbh "
                      "applicability class (extrapolation)",
        })

    status = (COMPARISON_COMPLETE if not incomplete_reasons
              else COMPARISON_INCOMPLETE)

    difference_payload = {
        "baseline_version_id": version.id,
        "baseline_label": version.label,
        "locked": {
            "t1_campaign": t1.code, "t2_campaign": t2.code,
            "identity_decisions_locked": True,
            "design_snapshot_locked": True,
            "baseline_equations_locked": True,
        },
        "source_attribution": (
            "Both runs use the identical survey rows, human identity "
            "decisions, pairings and design snapshot; the baseline re-run "
            "reproduces the confirmed result. Therefore every difference "
            "below is attributable to the equation change alone."
        ),
        "population": _population_diff(run_base["result"],
                                       run_cand["result"]),
        "plots": _plot_diff(run_base["result"], run_cand["result"]),
        "trees": coverage["tree_differences"],
        "candidate_run": {
            "species_without_equation":
                run_cand["result"]["species_without_equation"],
            "extrapolation_trees": [
                {"plot": p["plot"], "tree": e["tree"],
                 "dbh": e.get("dbh_t2", e.get("dbh_t1")),
                 "valid_range_cm": e["valid_range_cm"],
                 "equation": e["equation"]}
                for p in run_cand["result"]["provenance"]["plots"]
                for e in p["equation_range_extrapolations"]
            ],
        },
        "baseline_run": {
            "species_without_equation":
                run_base["result"]["species_without_equation"],
        },
        "candidate_result_checksum": equation_checksum(cand_map),
        "baseline_result_hash": base_hash,
        "candidate_result_hash": canonical_hash(run_cand["result"]),
    }

    comparison = ReviewComparison.objects.create(
        review=review, baseline_version=version, status=status,
        lock_fingerprints={**fingerprints,
                           "baseline_result": base_hash,
                           "candidate_result":
                               difference_payload["candidate_result_hash"],
                           "frame_snapshot": canonical_hash(
                               version.design_snapshot.get("strata"))},
        coverage_matrix={
            "rows": coverage["matrix"],
            "missing_species": coverage["missing_species"],
            "out_of_range_trees": coverage["out_of_range_trees"],
            "complete": status == COMPARISON_COMPLETE,
        },
        difference_payload=difference_payload,
        incomplete_reasons=incomplete_reasons,
        baseline_reproduced=True,
    )
    ReviewEvent.objects.create(
        review=review, event=ReviewEvent.EVENT_COMPARED, actor=actor,
        note=note or f"locked baseline {version.label}; coverage={status}",
        payload={"comparison_id": comparison.id,
                 "baseline_version_id": version.id, "status": status,
                 "incomplete_reasons": incomplete_reasons})
    return comparison


def _frame_snapshot_drift(version, strata_now, plots_now):
    snap = version.design_snapshot.get("strata")
    if not snap:
        return ["baseline design snapshot has no strata frame"]
    drift = []
    for code, s in snap.items():
        cur = strata_now.get(code)
        if cur is None:
            drift.append(f"stratum {code} vanished")
            continue
        if abs(cur["area_ha"] - s["area_ha"]) > 1e-9:
            drift.append(
                f"stratum {code} land area {s['area_ha']} -> {cur['area_ha']}")
        if sorted(cur["plot_codes"]) != sorted(s["plot_codes"]):
            drift.append(
                f"stratum {code} plot membership {s['plot_codes']} -> "
                f"{cur['plot_codes']}")
    extra = set(strata_now) - set(snap)
    if extra:
        drift.append(f"new strata appeared since lock: {sorted(extra)}")
    return drift


# --------------------------------------------------------------- validation
def validate_candidate(review, actor="", note=""):
    """Structural/domain validation of the immutable candidate spec."""
    if review.status != REVIEW_CANDIDATE:
        raise ReviewStateError(
            f"review is {review.status}; only candidates can be validated")

    spec = review.candidate_spec
    checks = []

    def add(name, passed, detail=""):
        checks.append({"name": name, "passed": bool(passed), "detail": detail})

    add("spec_nonempty", isinstance(spec, dict) and len(spec) > 0,
        f"{len(spec) if isinstance(spec, dict) else 0} species equations")

    existing_species = set(Species.objects.values_list("code", flat=True))
    finite = lambda v: isinstance(v, (int, float)) and math.isfinite(float(v))
    seen_code_versions = set()

    if isinstance(spec, dict) and spec:
        for sp, e in sorted(spec.items()):
            prefix = f"{sp}: "
            add(prefix + "species_exists", sp in existing_species)
            add(prefix + "finite_coefficients",
                all(finite(e.get(k)) for k in ("a", "b", "c"))
                and float(e.get("a", 0)) > 0 and float(e.get("b", 0)) > 0)
            add(prefix + "valid_dbh_range",
                finite(e.get("dbh_min_cm")) and finite(e.get("dbh_max_cm"))
                and 0 < e["dbh_min_cm"] <= e["dbh_max_cm"])
            add(prefix + "valid_residual_sigma",
                finite(e.get("residual_sigma"))
                and e["residual_sigma"] >= 0)
            add(prefix + "identified",
                bool(e.get("code")) and bool(e.get("version"))
                and bool(e.get("citation")))
            cv = (e.get("code"), e.get("version"))
            if cv[0]:
                seen_code_versions.add(cv)

        # candidate code/version must be NEW rows at approval — colliding
        # with an existing equation would mutate/confuse a locked equation.
        for code, version_no in sorted(seen_code_versions):
            collision = AllometricEquation.objects.filter(
                code=code, version=version_no).exists()
            add(f"new_code_version:{code}@{version_no}", not collision,
                "already exists" if collision else "free to create")

    # coverage preview against the two most recent campaigns (informational;
    # the authoritative coverage is computed against a LOCKED baseline).
    preview = _coverage_preview(spec)

    passed_all = all(c["passed"] for c in checks)
    payload = {"checks": checks, "coverage_preview": preview}
    if not passed_all:
        ReviewEvent.objects.create(
            review=review, event=ReviewEvent.EVENT_VALIDATED, actor=actor,
            note="validation FAILED: " + "; ".join(
                c["name"] for c in checks if not c["passed"]),
            payload=payload)
        raise ReviewValidationError(payload)

    review.validation_payload = payload
    review.status = REVIEW_VALIDATED
    review.validated_at = timezone.now()
    review.save()
    ReviewEvent.objects.create(
        review=review, event=ReviewEvent.EVENT_VALIDATED, actor=actor,
        note=note or "all structural/domain checks passed",
        payload=payload)
    return review


def _coverage_preview(spec):
    """Informational species/dbh coverage over the newest campaign pair."""
    from inventory.models import Campaign
    camps = list(Campaign.objects.order_by("measured_on"))
    if len(camps) < 2:
        return {"available": False}
    t1, t2 = camps[-2], camps[-1]
    try:
        t1t, t2t, _e, _p, _s = build_measurement_table(
            t1, t2, AllometricEquation.objects.none())
    except Exception:
        return {"available": False}
    rows = [r for r in t1t + t2t if r["dbh_cm"] is not None]
    per_species = defaultdict(lambda: {"n": 0, "min": None, "max": None})
    for r in rows:
        d = per_species[r["species"]]
        d["n"] += 1
        d["min"] = r["dbh_cm"] if d["min"] is None else min(d["min"], r["dbh_cm"])
        d["max"] = r["dbh_cm"] if d["max"] is None else max(d["max"], r["dbh_cm"])
    out = {"available": True, "t1": t1.code, "t2": t2.code, "species": {}}
    for sp, d in sorted(per_species.items()):
        e = spec.get(sp)
        out["species"][sp] = {
            "measurements": d["n"],
            "observed_dbh_range_cm": [round(d["min"], 2), round(d["max"], 2)],
            "candidate_present": e is not None,
            "candidate_covers_observed_range": (
                e is not None
                and e["dbh_min_cm"] <= d["min"]
                and e["dbh_max_cm"] >= d["max"]),
        }
    return out


# ------------------------------------------------------------------ approval
class ReviewError(Exception):
    pass


class ReviewStateError(ReviewError):
    pass


class ReviewValidationError(ReviewError):
    def __init__(self, payload):
        super().__init__("candidate validation failed")
        self.payload = payload


class ReviewLockError(ReviewError):
    def __init__(self, detail):
        super().__init__("baseline lock failed")
        self.detail = detail


def _event(review, kind, actor, note, payload=None):
    return ReviewEvent.objects.create(
        review=review, event=kind, actor=actor, note=note, payload=payload)


def approve_review(review, actor="", comparison=None, note=""):
    """
    Adopt the candidate: create NEW equation rows + a NEW confirmed
    EstimateVersion. Returns (review, version). Concurrency-safe: at most one
    version per review (select_for_update + unique constraint).
    """
    if review.status == REVIEW_WITHDRAWN:
        raise ReviewStateError("review was withdrawn; it cannot be approved")
    if review.status == REVIEW_APPROVED:
        raise ReviewStateError("review already approved")
    if review.status != REVIEW_VALIDATED:
        raise ReviewStateError("review must be validated before approval")

    if comparison is None:
        comparison = review.comparisons.first()
    if comparison is None:
        raise ReviewStateError(
            "no impact comparison exists; lock a confirmed baseline first")
    if comparison.review_id != review.id:
        raise ReviewStateError("comparison belongs to another review")
    if comparison.status != COMPARISON_COMPLETE:
        _event(review, ReviewEvent.EVENT_APPROVAL_REJECTED, actor,
               "approval blocked: comparison is incomplete",
               {"comparison_id": comparison.id,
                "reasons": comparison.incomplete_reasons})
        raise ReviewStateError(
            "comparison coverage is incomplete; approval is forbidden")

    version = comparison.baseline_version
    t1, t2 = version.t1_campaign, version.t2_campaign

    # re-verify the lock: data/identity/frame must be unchanged AND the
    # candidate must still produce exactly the compared result.
    fingerprints_now = current_fingerprints(t1, t2)
    stored_fp = comparison.lock_fingerprints
    drift = [k for k in ("measurements", "identity_conflicts",
                         "tree_identity", "plots", "strata", "campaigns")
             if fingerprints_now[k] != stored_fp.get(k)]
    run_base = run_version_inputs(version, version.equations.all())
    base_hash = canonical_hash(run_base["result"])
    if base_hash != stored_fp.get("baseline_result"):
        drift.append("baseline_result_not_reproducible")
    cand_map = spec_map_from_payload(review.candidate_spec)
    run_cand = run_version_inputs(version, cand_map)
    cand_hash = canonical_hash(run_cand["result"])
    if cand_hash != stored_fp.get("candidate_result"):
        drift.append("candidate_result_changed")
    frame_drift = _frame_snapshot_drift(
        version, run_base["strata"], run_base["plots"])
    if drift or frame_drift:
        _event(review, ReviewEvent.EVENT_APPROVAL_REJECTED, actor,
               "approval blocked: locked inputs drifted since comparison",
               {"drift": drift, "frame_drift": frame_drift,
                "comparison_id": comparison.id})
        raise ReviewLockError({"drift": drift, "frame_drift": frame_drift})

    from django.db import IntegrityError, OperationalError

    try:
        with transaction.atomic():
            # Row lock serialises concurrent approvers on PostgreSQL; sqlite
            # (dev) ignores it, but the OneToOne unique constraint on
            # generated_by_review still guarantees one version per review.
            locked = (EquationAdoptionReview.objects
                      .select_for_update()
                      .get(pk=review.pk))
            if locked.status != REVIEW_VALIDATED:
                raise ReviewStateError(
                    f"review is {locked.status} (concurrent decision)")
            if EstimateVersion.objects.filter(
                    generated_by_review=review).exists():
                raise ReviewStateError("a version already exists for review")

            # --- mint NEW equation rows (never edit locked ones) ----------
            grouped = {}
            for sp, e in spec_map_from_payload(review.candidate_spec).items():
                key = (e["code"], e["version"])
                grouped.setdefault(key, {"fields": e, "species": []})
                grouped[key]["species"].append(sp)

            new_equation_ids = []
            for (code, ver), bundle in grouped.items():
                f = bundle["fields"]
                eq = AllometricEquation.objects.create(
                    code=code, version=ver, status=VERSION_CONFIRMED,
                    form=f.get("form",
                               "agb = a * dbh_cm^b * height_m^c"),
                    a=f["a"], b=f["b"], c=f["c"],
                    dbh_min_cm=f["dbh_min_cm"], dbh_max_cm=f["dbh_max_cm"],
                    height_required=f["height_required"],
                    residual_sigma=f["residual_sigma"],
                    citation=f["citation"])
                eq.species.set(Species.objects.filter(
                    code__in=bundle["species"]))
                new_equation_ids.append(eq.id)

            # A COMPLETE comparison guarantees the candidate covers every
            # species that contributes biomass, so the adopted edition
            # references ONLY the freshly minted candidate equation rows.
            # This keeps its M2M set, equation_checksum and result_payload
            # mutually consistent. Baseline species with no eligible trees
            # contribute nothing and need not be carried onto the new edition.
            used_ids = sorted(new_equation_ids)

            snap = dict(version.design_snapshot)
            snap["equation_ids"] = used_ids
            snap["equation_codes"] = {
                sp: f"{e['code']}@{e['version']}"
                for sp, e in sorted(cand_map.items())}
            snap["adopted_from_review_id"] = review.id
            snap["adopted_from_version_id"] = version.id
            snap["adoption_lock_fingerprints"] = stored_fp

            new_version = EstimateVersion.objects.create(
                label=f"{review.label} (adopted candidate)",
                t1_campaign=t1, t2_campaign=t2,
                status=VERSION_CONFIRMED,
                design_snapshot=snap,
                result_payload=run_cand["result"],
                equation_checksum=equation_checksum(cand_map),
                confirmed_at=timezone.now(),
                generated_by_review=review,
            )
            new_version.equations.set(
                AllometricEquation.objects.filter(id__in=used_ids))

            review.status = REVIEW_APPROVED
            review.approved_at = timezone.now()
            review.save()
            _event(review, ReviewEvent.EVENT_APPROVED, actor,
                   note or "candidate adopted; new confirmed version minted",
                   {"new_version_id": new_version.id,
                    "baseline_version_id": version.id,
                    "new_equation_ids": new_equation_ids,
                    "comparison_id": comparison.id})
            return review, new_version
    except IntegrityError as exc:
        # Unique constraint collision: either the review already has a
        # generated version, or another transaction created the same
        # equation code/version first. In both cases exactly one version
        # survives the transaction (it was rolled back here).
        existing = EstimateVersion.objects.filter(
            generated_by_review=review).select_related().first()
        if existing is not None:
            raise ConcurrentApprovalError(existing) from exc
        existing = EstimateVersion.objects.filter(
            generated_by_review_id=review.pk).first()
        if existing is not None:
            raise ConcurrentApprovalError(existing) from exc
        # Some other integrity error (not a concurrent approval).
        raise
    except OperationalError as exc:  # e.g. sqlite "database is locked"
        existing = EstimateVersion.objects.filter(
            generated_by_review_id=review.pk).first()
        if existing:
            raise ConcurrentApprovalError(existing) from exc
        raise


def withdraw_review(review, actor="", note=""):
    if review.status in (REVIEW_APPROVED, REVIEW_WITHDRAWN):
        raise ReviewStateError(
            f"review is {review.status}; withdrawal is not allowed")
    review.status = REVIEW_WITHDRAWN
    review.withdrawn_at = timezone.now()
    review.save()
    _event(review, ReviewEvent.EVENT_WITHDRAWN, actor,
           note or "candidate withdrawn; comparisons retained for audit",
           {"comparisons_retained": review.comparisons.count()})
    return review


class ConcurrentApprovalError(ReviewError):
    def __init__(self, existing_version):
        self.existing_version = existing_version
        super().__init__(
            f"concurrent approval; version #{existing_version.id} is the "
            "single adopted version")
