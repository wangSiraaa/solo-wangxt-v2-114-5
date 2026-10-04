"""
Equation-adoption review service.

Purpose
=======
When a research team proposes a NEW version of an allometric equation it
must prove that any difference versus an existing CONFIRMED estimate is
caused by the equation itself — not by edited survey data, a changed
identity decision, or sampling-frame drift.

This module implements:

  candidate -> validated -> approved | withdrawn

* ``validate_candidate``      structural + empirical sanity checks;
* ``build_lock_snapshot``     freeze ONE baseline EstimateVersion's survey
                              data, human identity decisions and design;
* ``run_comparison``          rerun the estimator on the LOCKED frame with
                              baseline vs candidate equations, producing
                              per-tree / per-plot / population differences,
                              a coverage matrix and an explicit
                              difference-source (drift) statement;
* ``approve_review``          generate one NEW confirmed EstimateVersion
                              (and one new locked AllometricEquation) with
                              a database-level mutex against concurrent
                              approval; the baseline version is never
                              touched;
* ``withdraw_candidate``      kill a proposal while keeping its comparisons
                              as audit evidence that can never be confirmed.

The locked snapshot makes every comparison reproducible: rows, identity
pairs, plots, strata and design parameters are copied out of the baseline
and checksummed, so comparison never reads "current" data.
"""
import hashlib
import json
import math

from django.db import IntegrityError, transaction
from django.utils import timezone

from inventory.models import (
    AllometricEquation,
    CANDIDATE_APPROVED,
    CANDIDATE_VALIDATED,
    CANDIDATE_WITHDRAWN,
    CandidateEvent,
    COVERAGE_COMPLETE,
    COVERAGE_INCOMPLETE,
    EquationCandidate,
    EquationReview,
    EstimateVersion,
    ReviewApprovalSlot,
    ReviewEvent,
    REVIEW_APPROVED,
    REVIEW_OPEN,
    REVIEW_WITHDRAWN,
    VERSION_CONFIRMED,
)
from inventory.services.estimator import (
    _extrapolates,
    _has_eq_inputs,
    biomass_kg,
    build_plots_strata,
    equation_checksum,
    estimate,
    measurement_rows_for_campaign,
    resolved_identity_pairs,
)
from inventory.services.identity import pair_measurements

KG_PER_MG = 1000.0

# Baseline rerun must reproduce the frozen numbers essentially exactly
# (same pure estimator, same floats round-tripped through JSON).
REPRO_ABS_TOL_KG = 1e-6
REPRO_REL_TOL = 1e-9
BIN_WIDTH_CM = 5.0


class AdoptionError(Exception):
    """Workflow-level error. ``http_status`` carries the API response code."""

    def __init__(self, detail, http_status=400):
        super().__init__(detail)
        self.detail = detail
        self.http_status = http_status


# ------------------------------------------------------------- small helpers
def candidate_param_dict(candidate):
    """Equation-parameter dict in the shape estimator.estimate expects."""
    params = {}
    for sp in candidate.species.all():
        params[sp.code] = {
            "code": candidate.code,
            "version": candidate.version,
            "a": candidate.a, "b": candidate.b, "c": candidate.c,
            "dbh_min_cm": candidate.dbh_min_cm,
            "dbh_max_cm": candidate.dbh_max_cm,
            "height_required": candidate.height_required,
            "residual_sigma": candidate.residual_sigma,
            "citation": candidate.citation,
        }
    return params


def candidate_checksum(candidate):
    return equation_checksum(candidate_param_dict(candidate))


def canonical_json(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def _event_review(review, event, note="", payload=None, actor="station-console"):
    ReviewEvent.objects.create(review=review, event=event, actor=actor,
                               note=note, payload=payload)


def _event_candidate(candidate, event, note="", payload=None,
                     actor="station-console"):
    CandidateEvent.objects.create(candidate=candidate, event=event,
                                  actor=actor, note=note, payload=payload)


# ------------------------------------------------------------- candidate CRUD
def create_candidate(data, actor="station-console"):
    """Create a proposal; code/version must be new to BOTH equation tables."""
    species_codes = data.get("species_codes") or []
    if not species_codes:
        raise AdoptionError("a candidate must name at least one applicable "
                            "species.")
    from inventory.models import Species
    species = list(Species.objects.filter(code__in=species_codes))
    if len(species) != len(set(species_codes)):
        raise AdoptionError("unknown species code in species_codes.")
    code, version = data["code"], data["version"]
    if AllometricEquation.objects.filter(code=code, version=version).exists():
        raise AdoptionError(
            f"{code}@{version} already exists as a registered equation; a "
            "candidate is a NEW coefficient set and needs a new code/version.",
            http_status=409)
    if EquationCandidate.objects.filter(code=code, version=version).exists():
        raise AdoptionError(f"candidate {code}@{version} already exists.",
                            http_status=409)

    candidate = EquationCandidate.objects.create(
        code=code, version=version,
        form=data.get("form", "agb = a * dbh_cm^b * height_m^c"),
        a=data["a"], b=data["b"], c=data["c"],
        dbh_min_cm=data["dbh_min_cm"], dbh_max_cm=data["dbh_max_cm"],
        height_required=data.get("height_required", True),
        residual_sigma=data["residual_sigma"],
        citation=data.get("citation", ""),
    )
    candidate.species.set(species)
    _event_candidate(candidate, "created",
                     payload={"species_codes": sorted(species_codes)},
                     actor=actor)
    return candidate


def validate_candidate(candidate, actor="station-console"):
    """Run structural + empirical checks; candidate -> validated if all pass."""
    if candidate.status == CANDIDATE_WITHDRAWN:
        raise AdoptionError("candidate withdrawn; it cannot be validated.",
                            http_status=409)
    if candidate.status == CANDIDATE_APPROVED:
        raise AdoptionError("candidate already approved.", http_status=409)
    if candidate.status == CANDIDATE_VALIDATED:
        raise AdoptionError("candidate already validated.", http_status=409)

    checks = []

    def check(name, passed, detail=""):
        checks.append({"name": name, "passed": bool(passed), "detail": detail})
        return passed

    species = list(candidate.species.all())
    check("has_applicable_species", len(species) > 0,
          ", ".join(s.code for s in species))
    check("positive_scaling_a", math.isfinite(candidate.a) and candidate.a > 0,
          f"a={candidate.a}")
    check("nonnegative_exponents",
          math.isfinite(candidate.b) and math.isfinite(candidate.c)
          and candidate.b > 0 and candidate.c >= 0,
          f"b={candidate.b}, c={candidate.c}")
    check("finite_dbh_range",
          math.isfinite(candidate.dbh_min_cm)
          and math.isfinite(candidate.dbh_max_cm),
          f"{candidate.dbh_min_cm}-{candidate.dbh_max_cm} cm")
    check("ordered_dbh_range",
          0 < candidate.dbh_min_cm < candidate.dbh_max_cm)
    check("residual_sigma_nonneg",
          math.isfinite(candidate.residual_sigma)
          and candidate.residual_sigma >= 0,
          f"sigma={candidate.residual_sigma}")
    check("height_requirement_consistent",
          (not candidate.height_required) or candidate.c >= 0,
          "height_required with c>=0")
    check("citation_present", bool(candidate.citation.strip()),
          candidate.citation)

    # Empirical: evaluate the proposed form across the CURRENT measurement
    # frame. This is a sanity gate (finite, positive), not a comparison —
    # the locked comparison is where coverage is decided.
    frame_summary = _candidate_frame_summary(candidate, species)
    emp_ok = True
    for sp_code, summ in frame_summary["by_species"].items():
        eval_points = sorted({
            candidate.dbh_min_cm,
            (candidate.dbh_min_cm + candidate.dbh_max_cm) / 2.0,
            candidate.dbh_max_cm,
        })
        for d in eval_points:
            try:
                val = float(biomass_kg([d], [summ["reference_height_m"]],
                                       candidate_param_dict(candidate)[sp_code])[0])
            except Exception as exc:  # numeric failure is a failed check
                val = float("nan")
                check("numeric_evaluation", False,
                      f"{sp_code} dbh={d}: {exc}")
                emp_ok = False
                break
            if not math.isfinite(val) or val <= 0:
                check("numeric_evaluation", False,
                      f"{sp_code} dbh={d} -> {val} kg")
                emp_ok = False
                break
        else:
            continue
        break
    else:
        check("numeric_evaluation", emp_ok,
              "finite positive AGB at min/mid/max dbh for every species")

    passed = all(c["passed"] for c in checks)
    result = {
        "passed": passed,
        "checks": checks,
        "frame_summary": frame_summary,
        "validated_at": timezone.now().isoformat(),
    }
    candidate.validation_result = result
    if not passed:
        candidate.save(update_fields=["validation_result"])
        _event_candidate(candidate, "validation_failed",
                         payload={"failed": [c["name"] for c in checks
                                             if not c["passed"]]},
                         actor=actor)
        raise AdoptionError({"detail": "validation failed",
                             "validation_result": result})

    candidate.status = CANDIDATE_VALIDATED
    candidate.validated_at = timezone.now()
    candidate.save()
    _event_candidate(candidate, "validated",
                     payload={"checks": checks,
                              "frame_summary": frame_summary}, actor=actor)
    return candidate


def _candidate_frame_summary(candidate, species):
    """Current-frame stem counts/dbh range per covered species (informational)."""
    from inventory.models import STATUS_ALIVE_MEASURED, TreeMeasurement
    by_species = {}
    for sp in species:
        dbhs = list(TreeMeasurement.objects.filter(
            tree__species=sp, status=STATUS_ALIVE_MEASURED,
            dbh_cm__isnull=False,
        ).values_list("dbh_cm", flat=True))
        heights = [h for h in TreeMeasurement.objects.filter(
            tree__species=sp, status=STATUS_ALIVE_MEASURED,
            height_m__isnull=False,
        ).values_list("height_m", flat=True)]
        ref_h = max(heights) if heights else 1.3
        in_range = [d for d in dbhs
                    if candidate.dbh_min_cm <= d <= candidate.dbh_max_cm]
        by_species[sp.code] = {
            "n_measured_stems": len(dbhs),
            "frame_dbh_min_cm": (min(dbhs) if dbhs else None),
            "frame_dbh_max_cm": (max(dbhs) if dbhs else None),
            "n_in_candidate_range": len(in_range),
            "n_outside_candidate_range": len(dbhs) - len(in_range),
            "reference_height_m": ref_h,
        }
    return {"by_species": by_species}


@transaction.atomic
def withdraw_candidate(candidate, note="", actor="station-console"):
    """candidate (and its OPEN reviews) -> withdrawn; comparisons are kept."""
    candidate = EquationCandidate.objects.select_for_update().get(
        pk=candidate.pk)
    if candidate.status == CANDIDATE_WITHDRAWN:
        raise AdoptionError("candidate already withdrawn.", http_status=409)
    if candidate.status == CANDIDATE_APPROVED:
        raise AdoptionError(
            "an approved candidate cannot be withdrawn; the adopted "
            "equation/version stands. Retire the equation instead.",
            http_status=409)
    open_reviews = list(candidate.reviews.select_for_update().filter(
        status=REVIEW_OPEN))
    now = timezone.now()
    for review in open_reviews:
        # Bypass the immutable-save guard: withdrawal is a guarded lifecycle
        # transition, not an edit of the lock/comparison payload.
        EquationReview.objects.filter(pk=review.pk).update(
            status=REVIEW_WITHDRAWN, withdrawn_at=now)
        _event_review(review, "withdrawn",
                      note="candidate withdrawn; comparison retained for "
                           "audit and can never be confirmed",
                      actor=actor)
    candidate.status = CANDIDATE_WITHDRAWN
    candidate.withdrawn_at = now
    candidate.save(update_fields=["status", "withdrawn_at"])
    _event_candidate(candidate, "withdrawn", note=note, actor=actor,
                     payload={"review_ids": [r.id for r in open_reviews]})
    candidate.refresh_from_db()
    return candidate, open_reviews


# ------------------------------------------------------------- locked frame
def baseline_equations_for_version(version):
    """species-code -> locked equation params for a confirmed version."""
    eqs = version.equations.all().prefetch_related("species")
    return _mapping_from_equation_qs(eqs)


def _mapping_from_equation_qs(eqs):
    from inventory.services.estimator import equations_mapping_from_qs
    return equations_mapping_from_qs(eqs)


def build_lock_snapshot(version, actor="station-console"):
    """
    Copy a baseline version's frame: survey rows, identity decisions,
    plots/strata/design and the frozen equations. Nothing here is read
    again at comparison time.
    """
    if version.status != VERSION_CONFIRMED:
        raise AdoptionError(
            "the baseline of an adoption review must be a CONFIRMED "
            "EstimateVersion (its numbers and equations are frozen).",
            http_status=409)
    t1, t2 = version.t1_campaign, version.t2_campaign
    rows_t1 = measurement_rows_for_campaign(t1, t2)
    rows_t2 = measurement_rows_for_campaign(t2, t2)
    plots, _strata_now = build_plots_strata()
    renumber, distinct = resolved_identity_pairs(t1, t2)

    from inventory.models import IdentityConflict
    conflicts = []
    for c in IdentityConflict.objects.filter(
            t1_campaign=t1, t2_campaign=t2).select_related(
            "t1_measurement__tree", "t2_measurement__tree"):
        conflicts.append({
            "id": c.id,
            "plot": c.plot.code,
            "field_number": c.field_number,
            "t1_tree_id": c.t1_measurement.tree_id,
            "t2_tree_id": c.t2_measurement.tree_id,
            "status": c.status,
            "distance_m": c.distance_m,
        })
    # field-book verified renumber links living on Tree rows
    superseded_links = sorted(
        {tuple(k) for k in renumber
         if not any(c["t1_tree_id"] == k[0] and c["t2_tree_id"] == k[1]
                    and c["status"] == "renumber" for c in conflicts)}
        | {(r["tree_id"], r["verified_renumber_of"])
           for r in rows_t2 if r.get("verified_renumber_of")}
    )

    eqs_qs = version.equations.all().prefetch_related("species")
    eq_by_species = _mapping_from_equation_qs(eqs_qs)
    snapshot = {
        "t1_campaign": t1.code,
        "t2_campaign": t2.code,
        "locked_at": timezone.now().isoformat(),
        "locked_by": actor,
        "design": version.design_snapshot,
        "plots": plots,
        "rows_t1": rows_t1,
        "rows_t2": rows_t2,
        "identity": {
            "renumber_pairs": sorted(map(list, renumber)),
            "distinct_pairs": sorted(map(list, distinct)),
            "superseded_links": [list(k) for k in superseded_links],
            "conflicts": conflicts,
        },
        "baseline": {
            "version_id": version.id,
            "label": version.label,
            "equation_ids": sorted(eqs_qs.values_list("id", flat=True)),
            "equations_by_species": eq_by_species,
            "equation_checksum": version.equation_checksum,
            "result": version.result_payload,
        },
    }
    snapshot["lock_checksum"] = _snapshot_checksum(snapshot)
    return snapshot


def _snapshot_checksum(snapshot):
    material = {
        "t1": snapshot["t1_campaign"], "t2": snapshot["t2_campaign"],
        "design": snapshot["design"], "plots": snapshot["plots"],
        "rows_t1": snapshot["rows_t1"], "rows_t2": snapshot["rows_t2"],
        "identity": snapshot["identity"],
        "equations": snapshot["baseline"]["equations_by_species"],
    }
    return hashlib.sha256(canonical_json(material).encode()).hexdigest()


def create_review(candidate, baseline, label="", actor="station-console"):
    if candidate.status != CANDIDATE_VALIDATED:
        raise AdoptionError(
            f"candidate must be validated before a review (is "
            f"{candidate.status}).", http_status=409)
    dup = EquationReview.objects.filter(
        candidate=candidate, baseline_version=baseline,
        status=REVIEW_OPEN).exists()
    if dup:
        raise AdoptionError(
            "an open review already exists for this candidate and baseline.",
            http_status=409)
    snapshot = build_lock_snapshot(baseline, actor=actor)
    review = EquationReview.objects.create(
        label=label or f"{candidate.code}@{candidate.version} vs "
                       f"#{baseline.id} {baseline.label}",
        candidate=candidate, baseline_version=baseline,
        lock_snapshot=snapshot, lock_checksum=snapshot["lock_checksum"])
    _event_review(review, "created",
                  note="baseline survey data, identity decisions and design "
                       "snapshot locked",
                  payload={"baseline_version_id": baseline.id,
                           "lock_checksum": review.lock_checksum},
                  actor=actor)
    return review


# ------------------------------------------------------------- comparison
def _frame_species(rows1, rows2):
    return sorted({r["species"] for r in rows1 + rows2})


def _bin(dbh):
    lo = math.floor(dbh / BIN_WIDTH_CM) * BIN_WIDTH_CM
    return f"{lo:g}-{lo + BIN_WIDTH_CM:g}"


def _iter_tree_records(pairing, recruitment_cm):
    """
    Mirror estimator.evaluate semantics: yield one record per tree-OCCASION
    that the estimator quantifies (or explicitly excludes).
    """
    def rec(pair_or_row, role):
        raise NotImplementedError  # replaced inline below

    for pair in pairing["pairs"]:
        r1, r2 = pair["t1"], pair["t2"]
        if r2["status"] == "dead":
            yield {"tag": f"{r1['plot']}/{r2['field_number']}",
                   "plot": r1["plot"], "species": r1["species"],
                   "role": "mortality_t1", "occasion": "t1",
                   "dbh_cm": r1["dbh_cm"], "height_m": r1["height_m"],
                   "pair_kind": pair["kind"]}
        elif r2["status"] == "missing_tree":
            yield {"tag": f"{r1['plot']}/{r2['field_number']}",
                   "plot": r1["plot"], "species": r1["species"],
                   "role": "not_located", "occasion": None,
                   "dbh_cm": None, "height_m": None, "pair_kind": pair["kind"]}
        elif r2["status"] == "alive_not_measured":
            yield {"tag": f"{r1['plot']}/{r2['field_number']}",
                   "plot": r1["plot"], "species": r1["species"],
                   "role": "survivor_missing_t1", "occasion": "t1",
                   "dbh_cm": r1["dbh_cm"], "height_m": r1["height_m"],
                   "pair_kind": pair["kind"]}
        elif r2["status"] == "alive_measured":
            for occ, r, role in (("t1", r1, "survivor_t1"),
                                 ("t2", r2, "survivor_t2")):
                yield {"tag": f"{r['plot']}/{r2['field_number']}",
                       "plot": r["plot"], "species": r["species"],
                       "role": role, "occasion": occ,
                       "dbh_cm": r["dbh_cm"], "height_m": r["height_m"],
                       "pair_kind": pair["kind"]}

    for r1 in pairing["t1_only"]:
        yield {"tag": f"{r1['plot']}/{r1['field_number']}",
               "plot": r1["plot"], "species": r1["species"],
               "role": "mortality_t1", "occasion": "t1",
               "dbh_cm": r1["dbh_cm"], "height_m": r1["height_m"],
               "pair_kind": "t1_only_distinct"}
    for r2 in pairing["t2_only"]:
        role = ("ingrowth_t2" if (r2["status"] == "alive_measured"
                                  and r2["dbh_cm"] is not None
                                  and r2["dbh_cm"] >= recruitment_cm)
                else "below_recruitment_t2"
                if r2["status"] == "alive_measured"
                else "t2_only_unquantified")
        yield {"tag": f"{r2['plot']}/{r2['field_number']}",
               "plot": r2["plot"], "species": r2["species"],
               "role": role, "occasion": "t2" if role == "ingrowth_t2" else None,
               "dbh_cm": r2["dbh_cm"], "height_m": r2["height_m"],
               "pair_kind": "t2_only_distinct"}


# roles the estimator turns into biomass on a named occasion
QUANTIFIED_ROLES = {
    "mortality_t1": "t1",
    "survivor_t1": "t1",
    "survivor_t2": "t2",
    "survivor_missing_t1": "t1",
    "ingrowth_t2": "t2",
}


def _evaluate_record(rec, base_map, cand_params, candidate_species):
    """Attach baseline/candidate AGB and applicability flags to a record."""
    out = dict(rec)
    role = rec["role"]
    species = rec["species"]
    base_eq = base_map.get(species)
    cand_eq = cand_params.get(species)
    out["candidate_covered_species"] = species in candidate_species

    quantified = role in QUANTIFIED_ROLES and base_eq is not None
    row_like = {"dbh_cm": rec["dbh_cm"], "height_m": rec["height_m"]}
    base_ok = quantified and _has_eq_inputs(row_like, base_eq)
    out["baseline_quantified"] = base_ok
    out["extrapolates_baseline"] = (
        base_ok and _extrapolates(row_like, base_eq))
    if base_ok:
        out["agb_baseline_kg"] = round(float(biomass_kg(
            [rec["dbh_cm"]], [rec["height_m"]], base_eq)[0]), 6)
        out["baseline_equation"] = f"{base_eq['code']}@{base_eq['version']}"
    else:
        out["agb_baseline_kg"] = None
        out["baseline_equation"] = (
            f"{base_eq['code']}@{base_eq['version']}" if base_eq else None)

    cand_ok = (quantified and cand_eq is not None
               and _has_eq_inputs(row_like, cand_eq))
    out["candidate_quantified"] = cand_ok
    out["extrapolates_candidate"] = (
        cand_ok and _extrapolates(row_like, cand_eq))
    # A coverage regression = baseline quantified this stem but the
    # candidate cannot (it demands an input that was absent). Stems the
    # baseline itself could not quantify are excluded from the charge.
    out["candidate_missing_input"] = (
        base_ok and cand_eq is not None and not cand_ok
        and not out["extrapolates_candidate"])
    if cand_ok:
        out["agb_candidate_kg"] = round(float(biomass_kg(
            [rec["dbh_cm"]], [rec["height_m"]], cand_eq)[0]), 6)
        out["candidate_equation"] = (
            f"{cand_eq['code']}@{cand_eq['version']}")
    else:
        out["agb_candidate_kg"] = None
        out["candidate_equation"] = (
            f"{cand_eq['code']}@{cand_eq['version']}" if cand_eq else None)

    if base_ok and cand_ok:
        out["delta_kg"] = round(out["agb_candidate_kg"]
                                - out["agb_baseline_kg"], 6)
    else:
        out["delta_kg"] = None
    return out


def _coverage_matrix(records, candidate_species, all_species):
    """
    species x 5-cm dbh bin matrix: stems quantified by the baseline and how
    the candidate covers them. Bins come from baseline-quantified dbhs.
    """
    matrix = {sp: {} for sp in all_species}
    for rec in records:
        sp = rec["species"]
        if (rec["role"] in QUANTIFIED_ROLES and rec["dbh_cm"] is not None
                and rec["baseline_quantified"]):
            label = _bin(rec["dbh_cm"])
            entry = matrix.setdefault(sp, {}).setdefault(label, {
                "dbh_min_cm": _bin_lo(rec["dbh_cm"]),
                "dbh_max_cm": _bin_lo(rec["dbh_cm"]) + BIN_WIDTH_CM,
                "n": 0, "covered": 0, "extrapolated": 0,
                "missing_species": 0, "missing_input": 0})
            entry["n"] += 1
            if sp not in candidate_species:
                entry["missing_species"] += 1
            elif rec["candidate_quantified"]:
                entry["covered"] += 1
                if rec["extrapolates_candidate"]:
                    entry["extrapolated"] += 1
            elif rec["candidate_missing_input"]:
                entry["missing_input"] += 1
    return {sp: {"bins": [dict(bin=k, **v)
                          for k, v in sorted(bins.items(),
                                             key=lambda kv: kv[1]["dbh_min_cm"])],
                 "species_covered": sp in candidate_species}
            for sp, bins in sorted(matrix.items())}


def _bin_lo(dbh):
    return math.floor(dbh / BIN_WIDTH_CM) * BIN_WIDTH_CM


def _population_section(base_result, cand_result):
    comps = {}
    for key in ("survivor_growth", "mortality", "ingrowth"):
        b, c = base_result["components"][key], cand_result["components"][key]
        comps[key] = {
            "baseline_mg": b["total_mg"],
            "candidate_mg": c["total_mg"],
            "delta_mg": round(c["total_mg"] - b["total_mg"], 9),
            "delta_percent": (round(100.0 * (c["total_kg"] - b["total_kg"])
                                    / b["total_kg"], 3)
                              if b["total_kg"] else None),
            "baseline_ci95_mg": [b["ci95_kg"][0] / KG_PER_MG,
                                 b["ci95_kg"][1] / KG_PER_MG],
            "candidate_ci95_mg": [c["ci95_kg"][0] / KG_PER_MG,
                                  c["ci95_kg"][1] / KG_PER_MG],
        }
    net_b = base_result["net_change"]
    net_c = cand_result["net_change"]
    stocks = {
        "t1": {"baseline_mg": base_result["stocks"]["t1_mg"],
               "candidate_mg": cand_result["stocks"]["t1_mg"],
               "delta_mg": round(cand_result["stocks"]["t1_mg"]
                                 - base_result["stocks"]["t1_mg"], 9)},
        "t2": {"baseline_mg": base_result["stocks"]["t2_mg"],
               "candidate_mg": cand_result["stocks"]["t2_mg"],
               "delta_mg": round(cand_result["stocks"]["t2_mg"]
                                 - base_result["stocks"]["t2_mg"], 9)},
    }
    return {
        "components": comps,
        "net_change": {
            "baseline_mg": net_b["total_mg"],
            "candidate_mg": net_c["total_mg"],
            "delta_mg": round(net_c["total_kg"] - net_b["total_kg"], 9),
            "candidate_se_total_kg": net_c["se_total_kg"],
            "baseline_se_total_kg": net_b["se_total_kg"],
        },
        "stocks": stocks,
    }


def _per_plot_section(base_result, cand_result, evaluated):
    """Per-plot kg + kg/ha deltas and candidate coverage flags."""
    bp = {p["plot"]: p for p in base_result["provenance"]["plots"]}
    cp = {p["plot"]: p for p in cand_result["provenance"]["plots"]}
    flags = {}
    for rec in evaluated:
        f = flags.setdefault(rec["plot"], {
            "candidate_extrapolations": 0,
            "missing_species_stems": 0,
            "missing_input_stems": 0,
        })
        if rec["extrapolates_candidate"]:
            f["candidate_extrapolations"] += 1
        if rec["role"] in QUANTIFIED_ROLES:
            if not rec["candidate_covered_species"]:
                f["missing_species_stems"] += 1
            elif rec["candidate_missing_input"]:
                f["missing_input_stems"] += 1
    out = []
    for code in sorted(bp):
        b, c = bp[code], cp.get(code, {})
        area = b["area_ha"]
        kg_b = b["kg"]
        kg_c = c.get("kg", {})
        rows = {}
        for key in ("survivor_growth", "mortality", "ingrowth",
                    "stock_t1", "stock_t2"):
            bv, cv = kg_b[key], kg_c.get(key, 0.0)
            rows[key] = {
                "baseline_kg": bv, "candidate_kg": round(cv, 6),
                "delta_kg": round(cv - bv, 6),
                "baseline_kg_ha": round(bv / area, 6),
                "candidate_kg_ha": round(cv / area, 6),
                "delta_kg_ha": round((cv - bv) / area, 6),
            }
        out.append({"plot": code, "stratum": b["stratum"], "area_ha": area,
                    "components": rows, **flags.get(code, {
                        "candidate_extrapolations": 0,
                        "missing_species_stems": 0,
                        "missing_input_stems": 0})})
    return out


def run_comparison(review, actor="station-console"):
    """
    Rerun baseline vs candidate on the LOCKED frame and freeze the result.
    The comparison is write-once: it is audit evidence.
    """
    review = EquationReview.objects.select_for_update().get(pk=review.pk)
    if review.status != REVIEW_OPEN:
        raise AdoptionError(
            f"review is {review.status}; comparison cannot (re)run.",
            http_status=409)
    if review.comparison is not None:
        raise AdoptionError(
            "comparison already run and frozen for this review.",
            http_status=409)
    candidate = review.candidate
    if candidate.status != CANDIDATE_VALIDATED:
        raise AdoptionError(
            f"candidate is {candidate.status}; only validated candidates "
            "can be compared.", http_status=409)

    snap = review.lock_snapshot
    design = dict(snap["design"])
    strata = design["strata"]
    plots = snap["plots"]
    rows1, rows2 = snap["rows_t1"], snap["rows_t2"]
    renumber = {tuple(k) for k in snap["identity"]["renumber_pairs"]}
    distinct = {tuple(k) for k in snap["identity"]["distinct_pairs"]}
    base_map = snap["baseline"]["equations_by_species"]

    cand_params = candidate_param_dict(candidate)
    candidate_species = set(cand_params)
    all_species = _frame_species(rows1, rows2)
    missing_species = sorted(set(all_species) - candidate_species)
    inherited = sorted(set(base_map) & set(missing_species))

    # Population run: uncovered species keep the LOCKED baseline equation
    # (flagged "inherited"); the estimator is otherwise untouched.
    cand_run_map = {sp: cand_params.get(sp, base_map[sp]) for sp in base_map}

    pairing = pair_measurements(
        rows1, rows2,
        resolved_renumber_pairs=renumber,
        resolved_distinct_pairs=distinct)
    records = [_evaluate_record(r, base_map, cand_params, candidate_species)
               for r in _iter_tree_records(pairing, design["recruitment_cm"])]
    evaluated = [r for r in records if r["role"] in QUANTIFIED_ROLES]

    base_result = estimate(rows1, rows2, base_map, plots, strata, design,
                           renumber, distinct)
    cand_result = estimate(rows1, rows2, cand_run_map, plots, strata, design,
                           renumber, distinct)

    # --- baseline reproduction: prove the rerun reproduces the frozen edition
    frozen = snap["baseline"]["result"]
    repro_checks = []
    for label, bval, fval in [
        ("survivor_growth",
         base_result["components"]["survivor_growth"]["total_kg"],
         frozen["components"]["survivor_growth"]["total_kg"]),
        ("mortality",
         base_result["components"]["mortality"]["total_kg"],
         frozen["components"]["mortality"]["total_kg"]),
        ("ingrowth",
         base_result["components"]["ingrowth"]["total_kg"],
         frozen["components"]["ingrowth"]["total_kg"]),
        ("stock_t1", base_result["stocks"]["t1_mg"] * KG_PER_MG,
         frozen["stocks"]["t1_mg"] * KG_PER_MG),
        ("stock_t2", base_result["stocks"]["t2_mg"] * KG_PER_MG,
         frozen["stocks"]["t2_mg"] * KG_PER_MG),
        ("net_change", base_result["net_change"]["total_kg"],
         frozen["net_change"]["total_kg"]),
    ]:
        diff = abs(bval - fval)
        rel = diff / max(abs(fval), 1e-12)
        ok = diff <= REPRO_ABS_TOL_KG or rel <= REPRO_REL_TOL
        repro_checks.append({"quantity": label, "abs_diff_kg": diff,
                             "rel_diff": rel, "passed": ok})
    repro_pass = all(c["passed"] for c in repro_checks)

    out_of_range = [
        {"tag": r["tag"], "species": r["species"], "role": r["role"],
         "dbh_cm": r["dbh_cm"],
         "candidate_range_cm": [cand_params[r["species"]]["dbh_min_cm"],
                                cand_params[r["species"]]["dbh_max_cm"]]}
        for r in evaluated
        if r["candidate_covered_species"] and r["extrapolates_candidate"]
    ]
    missing_input_regressions = [
        {"tag": r["tag"], "species": r["species"], "role": r["role"],
         "reason": "candidate requires inputs the baseline did not"}
        for r in evaluated
        if r["candidate_covered_species"] and r["candidate_missing_input"]
        and r["baseline_quantified"]
    ]

    coverage = COVERAGE_COMPLETE if (
        repro_pass
        and not missing_species
        and not out_of_range
        and not missing_input_regressions
    ) else COVERAGE_INCOMPLETE

    incomplete_reasons = []
    if not repro_pass:
        incomplete_reasons.append("baseline_reproduction_failed")
    if missing_species:
        incomplete_reasons.append("missing_species:" + ",".join(missing_species))
    if out_of_range:
        incomplete_reasons.append(f"dbh_outside_range:{len(out_of_range)}")
    if missing_input_regressions:
        incomplete_reasons.append(
            f"missing_required_inputs:{len(missing_input_regressions)}")

    by_species = {}
    role_totals = {}
    for r in evaluated:
        sp = by_species.setdefault(r["species"], {
            "n_occasions": 0, "baseline_kg": 0.0, "candidate_kg": 0.0,
            "delta_kg": 0.0, "extrapolated": 0,
            "candidate_missing_species":
                r["species"] not in candidate_species})
        sp["n_occasions"] += 1
        if r["baseline_quantified"]:
            sp["baseline_kg"] += r["agb_baseline_kg"]
        if r["candidate_quantified"]:
            sp["candidate_kg"] += r["agb_candidate_kg"]
        if r["delta_kg"] is not None:
            sp["delta_kg"] += r["delta_kg"]
        if r["extrapolates_candidate"]:
            sp["extrapolated"] += 1
        rt = role_totals.setdefault(r["role"], {
            "baseline_kg": 0.0, "candidate_kg": 0.0, "delta_kg": 0.0})
        if r["baseline_quantified"]:
            rt["baseline_kg"] += r["agb_baseline_kg"]
        if r["candidate_quantified"]:
            rt["candidate_kg"] += r["agb_candidate_kg"]
        if r["delta_kg"] is not None:
            rt["delta_kg"] += r["delta_kg"]
    for d in (by_species, role_totals):
        for v in d.values():
            for k in ("baseline_kg", "candidate_kg", "delta_kg"):
                v[k] = round(v[k], 6)

    comparison = {
        "compared_at": timezone.now().isoformat(),
        "baseline_version_id": snap["baseline"]["version_id"],
        "candidate": {"code": candidate.code, "version": candidate.version,
                      "checksum": candidate_checksum(candidate)},
        "lock_checksum": review.lock_checksum,
        "coverage": {
            "status": coverage,
            "complete": coverage == COVERAGE_COMPLETE,
            "incomplete_reasons": incomplete_reasons,
            "missing_species": missing_species,
            "inherited_baseline_species": inherited,
            "n_out_of_range": len(out_of_range),
            "out_of_range": out_of_range,
            "n_missing_input_regressions": len(missing_input_regressions),
            "missing_input_regressions": missing_input_regressions,
            "matrix": _coverage_matrix(records, candidate_species,
                                       all_species),
        },
        "diff_sources": {
            "data_drift": "none — survey rows are copied from the locked "
                          "baseline version, not re-read from the database",
            "identity_drift": "none — verified renumber/distinct decisions "
                              "and conflict states are locked",
            "frame_drift": "none — plots, stratum areas, recruitment "
                           "threshold and design parameters are locked",
            "equation_only": repro_pass,
            "baseline_reproduction": {
                "passed": repro_pass,
                "abs_tol_kg": REPRO_ABS_TOL_KG,
                "rel_tol": REPRO_REL_TOL,
                "checks": repro_checks,
            },
        },
        "population": _population_section(base_result, cand_result),
        "per_plot": _per_plot_section(base_result, cand_result, evaluated),
        "by_species": by_species,
        "role_totals_kg": role_totals,
        "per_tree": records,
        "excluded_identity_conflicts": [
            {"plot": (c.get("t1") or c.get("t2"))["plot"],
             "field_number": (c.get("t1") or c.get("t2"))["field_number"],
             "distance_m": c["distance_m"], "hint": c.get("hint")}
            for c in pairing["conflicts"]
        ],
        "candidate_equations_used": {
            sp: (f"{cand_params[sp]['code']}@{cand_params[sp]['version']}"
                 if sp in cand_params
                 else f"{base_map[sp]['code']}@{base_map[sp]['version']} (baseline, inherited)")
            for sp in all_species},
    }

    review.comparison = comparison
    review.coverage_status = coverage
    review.compared_at = timezone.now()
    review.candidate_checksum_at_comparison = comparison["candidate"]["checksum"]
    review.save()
    _event_review(
        review, "compared",
        note=(f"coverage {coverage}; net change delta "
              f"{comparison['population']['net_change']['delta_mg']} Mg; "
              + ("differences attributable to the equation only"
                 if repro_pass else "BASELINE REPRODUCTION FAILED")),
        payload={"coverage": coverage,
                 "incomplete_reasons": incomplete_reasons,
                 "net_delta_mg":
                     comparison["population"]["net_change"]["delta_mg"]},
        actor=actor)
    return review


# ------------------------------------------------------------- approval
@transaction.atomic
def approve_review(review, label=None, actor="station-console"):
    """
    Generate the NEW confirmed EstimateVersion. Exactly one new version per
    review even under concurrency (ReviewApprovalSlot unique constraint).
    The baseline version is never modified.
    """
    review = EquationReview.objects.select_for_update().get(pk=review.pk)
    candidate = EquationCandidate.objects.select_for_update().get(
        pk=review.candidate_id)
    baseline = EstimateVersion.objects.select_for_update().get(
        pk=review.baseline_version_id)

    if review.status == REVIEW_APPROVED:
        raise AdoptionError("review already approved; only one new version "
                            "is ever generated.", http_status=409)
    if review.status == REVIEW_WITHDRAWN:
        raise AdoptionError(
            "review was withdrawn; its comparison is retained for audit "
            "but cannot be confirmed.", http_status=409)
    if candidate.status == CANDIDATE_WITHDRAWN:
        raise AdoptionError("candidate withdrawn; approval forbidden.",
                            http_status=409)
    if candidate.status != CANDIDATE_VALIDATED:
        raise AdoptionError("candidate must be validated.", http_status=409)
    if review.comparison is None:
        raise AdoptionError("run the impact comparison before approval.",
                            http_status=409)
    if review.coverage_status != COVERAGE_COMPLETE:
        raise AdoptionError(
            "coverage is incomplete ("
            + "; ".join(review.comparison["coverage"]["incomplete_reasons"])
            + "); approval is forbidden until the candidate covers every "
              "species and dbh class in the locked frame.",
            http_status=409)
    if baseline.status != VERSION_CONFIRMED:
        raise AdoptionError("baseline version is no longer confirmed.",
                            http_status=409)

    # candidate coefficients must not have drifted since the comparison
    if candidate_checksum(candidate) != review.candidate_checksum_at_comparison:
        raise AdoptionError(
            "candidate changed after the comparison; rerun comparison on a "
            "new review.", http_status=409)
    # locked baseline equations must still checksum to what was compared
    current_base_checksum = equation_checksum(
        _mapping_from_equation_qs(
            baseline.equations.all().prefetch_related("species")))
    if current_base_checksum != snap_baseline_checksum(review):
        raise AdoptionError(
            "baseline equations differ from the locked comparison frame.",
            http_status=409)

    # Database mutex: a concurrent approval loses here and rolls back.
    try:
        with transaction.atomic():
            ReviewApprovalSlot.objects.create(review=review)
    except IntegrityError:
        raise AdoptionError(
            "review already approved by a concurrent request; only one new "
            "EstimateVersion is generated.", http_status=409)

    snap = review.lock_snapshot
    design = dict(snap["design"])
    strata = design["strata"]
    plots = snap["plots"]
    rows1, rows2 = snap["rows_t1"], snap["rows_t2"]
    renumber = {tuple(k) for k in snap["identity"]["renumber_pairs"]}
    distinct = {tuple(k) for k in snap["identity"]["distinct_pairs"]}

    # 1) new locked equation row from the approved candidate
    if AllometricEquation.objects.filter(
            code=candidate.code, version=candidate.version).exists():
        raise AdoptionError(
            f"{candidate.code}@{candidate.version} now exists as an "
            "equation; aborting to avoid a duplicate.", http_status=409)
    equation = AllometricEquation.objects.create(
        code=candidate.code, version=candidate.version,
        status="confirmed", form=candidate.form,
        a=candidate.a, b=candidate.b, c=candidate.c,
        dbh_min_cm=candidate.dbh_min_cm, dbh_max_cm=candidate.dbh_max_cm,
        height_required=candidate.height_required,
        residual_sigma=candidate.residual_sigma,
        citation=candidate.citation)
    equation.species.set(candidate.species.all())

    # 2) equation set for the new version: candidate species use the new
    #    equation; every other species keeps the LOCKED baseline equation.
    baseline_eqs = list(
        AllometricEquation.objects.filter(
            id__in=snap["baseline"]["equation_ids"]))
    candidate_species = set(cand_param_sp(candidate))
    retained = [e for e in baseline_eqs
                if not set(e.species.values_list("code", flat=True))
                & candidate_species]
    equation_set = retained + [equation]

    eq_map = _mapping_from_equation_qs(
        AllometricEquation.objects.filter(
            id__in=[e.id for e in equation_set]).prefetch_related("species"))
    result = estimate(rows1, rows2, eq_map, plots, strata, design,
                      renumber, distinct)
    checksum = equation_checksum(eq_map)
    result["species_without_equation"] = []
    result["adoption"] = {
        "generated_by_review_id": review.id,
        "baseline_version_id": baseline.id,
        "candidate_code": candidate.code,
        "candidate_version": candidate.version,
        "baseline_lock_checksum": review.lock_checksum,
        "frame": "identical to baseline: survey data, identity decisions "
                 "and design locked; only equations differ",
        "net_change_delta_vs_baseline_kg": round(
            result["net_change"]["total_kg"]
            - snap["baseline"]["result"]["net_change"]["total_kg"], 9),
    }

    new_design = {k: v for k, v in design.items()}
    new_design["equation_ids"] = sorted(e.id for e in equation_set)
    new_design["equation_codes"] = {
        sp: e["code"] + "@" + e["version"] for sp, e in eq_map.items()}
    new_design["adoption"] = {
        "baseline_version_id": baseline.id,
        "review_id": review.id,
        "lock_checksum": review.lock_checksum,
    }

    version = EstimateVersion.objects.create(
        label=label or f"{baseline.label} — adopted {candidate.code}@"
                       f"{candidate.version}",
        t1_campaign=baseline.t1_campaign, t2_campaign=baseline.t2_campaign,
        status=VERSION_CONFIRMED,
        design_snapshot=new_design, result_payload=result,
        equation_checksum=checksum, confirmed_at=timezone.now(),
        generated_by_review=review)
    version.equations.set(equation_set)

    candidate.status = CANDIDATE_APPROVED
    candidate.save(update_fields=["status"])
    EquationReview.objects.filter(pk=review.pk).update(
        status=REVIEW_APPROVED, approved_at=timezone.now())
    review.refresh_from_db()

    _event_review(review, "approved", actor=actor, payload={
        "new_version_id": version.id,
        "new_equation": f"{candidate.code}@{candidate.version}",
        "baseline_version_id": baseline.id,
        "baseline_unchanged": True,
        "net_delta_kg":
            result["adoption"]["net_change_delta_vs_baseline_kg"]})
    _event_candidate(candidate, "approved", actor=actor, payload={
        "review_id": review.id, "new_version_id": version.id,
        "new_equation_id": equation.id})
    return review, version, equation


def cand_param_sp(candidate):
    return [sp.code for sp in candidate.species.all()]


def snap_baseline_checksum(review):
    return review.lock_snapshot["baseline"]["equation_checksum"]
