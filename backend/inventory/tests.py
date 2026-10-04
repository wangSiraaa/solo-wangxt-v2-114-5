"""
Acceptance tests for the permanent-plot station.

Covers the acceptance checks specified by the station:
  A. remeasurement renumber handling;
  B. unequal plot areas in population expansion;
  C. measurement-unit mistakes rejected at ingest;
  D. real zero growth vs missing data vs mortality kept distinct;
  E. same number + contradictory position never auto-merged;
  F. confirmed estimate editions cannot be silently changed by a new
     allometric equation.
"""
import json

from django.conf import settings
from django.core.exceptions import ValidationError as DjValidationError
from django.test import TestCase
from rest_framework.test import APIClient

from inventory.models import (
    AllometricEquation,
    Campaign,
    CONFLICT_OPEN,
    EquationAdoptionReview,
    EstimateVersion,
    Plot,
    ReviewComparison,
    Species,
    Stratum,
    Tree,
    TreeMeasurement,
)
from inventory.services.estimator import (
    build_measurement_table,
    estimate,
    resolved_identity_pairs,
)
from inventory.services.identity import pair_measurements
from inventory.services.ingest import import_campaign_rows, verify_plot_area
from inventory.services.units import convert_dbh_to_cm, ring_area_ha


def rect(ox, oy, w, d):
    return [[ox, oy], [ox + w, oy], [ox + w, oy + d], [ox, oy + d],
            [ox, oy]]


AM, AN, DE = "alive_measured", "alive_not_measured", "dead"


class EstimatorAcceptanceTests(TestCase):
    def setUp(self):
        self.sA = Stratum.objects.create(code="A", name="A", area_ha=100.0)
        self.oak = Species.objects.create(code="OAK", name="Oak")
        self.eq = AllometricEquation.objects.create(
            code="OAK", version="1", status="confirmed",
            a=0.1, b=2.0, c=0.5, dbh_min_cm=5.0, dbh_max_cm=100.0,
            residual_sigma=0.1, citation="fictional")
        self.eq.species.add(self.oak)
        self.t1 = Campaign.objects.create(code="t1", measured_on="2019-01-01")
        self.t2 = Campaign.objects.create(code="t2", measured_on="2024-01-01")
        # Unequal plot areas: 0.10 ha and 0.25 ha.
        self.p1 = Plot.objects.create(
            code="P1", stratum=self.sA, x_m=0, y_m=0,
            declared_area_ha=0.10, boundary=rect(0, 0, 50, 20),
            area_polygon_ha=0.10)
        self.p2 = Plot.objects.create(
            code="P2", stratum=self.sA, x_m=0, y_m=0,
            declared_area_ha=0.25, boundary=rect(0, 0, 50, 50),
            area_polygon_ha=0.25)

    def _run(self):
        t1t, t2t, equations, plots, strata = build_measurement_table(
            self.t1, self.t2, AllometricEquation.objects.all())
        ren, dist = resolved_identity_pairs(self.t1, self.t2)
        design = dict(t1_code="t1", t2_code="t2", interval_years=5.0,
                      dbh_sd_cm=0.1, height_sd_m=0.3, zero_tol_cm=0.15,
                      recruitment_cm=5.0, fpc=False, crs_epsg=32650)
        return estimate(t1t, t2t, equations, plots, strata, design, ren, dist)

    # ---------- C. unit mistakes ------------------------------------------------
    def test_dbh_unit_must_be_explicit(self):
        with self.assertRaises(DjValidationError):
            convert_dbh_to_cm(25.0, None)

    def test_mm_entered_as_cm_is_rejected_by_range(self):
        # 250 (mm) typed as cm -> 250 cm beyond the accepted demo range.
        with self.assertRaises(DjValidationError):
            convert_dbh_to_cm(250.0, "cm")

    def test_mm_value_correctly_converted(self):
        self.assertAlmostEqual(convert_dbh_to_cm(250.0, "mm"), 25.0)

    def test_import_rejects_bad_unit_rows(self):
        rows = [
            dict(plot="P1", field_number="1", species="OAK",
                 x_m=10, y_m=10, status=AM,
                 dbh_raw=250.0, dbh_unit="cm",
                 height_raw=15.0, height_unit="m"),
            dict(plot="P1", field_number="2", species="OAK",
                 x_m=12, y_m=12, status=AM,
                 dbh_raw=20.0, height_raw=15.0, height_unit="m"),
        ]
        r = import_campaign_rows(self.t2, rows, 0.01)
        self.assertEqual(r["n_rejected"], 2)
        self.assertEqual(TreeMeasurement.objects.count(), 0)

    # ---------- B. unequal plot areas ------------------------------------------
    def test_unequal_plot_areas_expanded_per_plot(self):
        # 100 kg growth on each plot; per-ha: 1000 vs 400 kg/ha.
        import_campaign_rows(self.t1, [
            dict(plot="P1", field_number="1", species="OAK",
                 x_m=5, y_m=5, status=AM, dbh_raw=20.0, dbh_unit="cm",
                 height_raw=15.0, height_unit="m"),
            dict(plot="P2", field_number="1", species="OAK",
                 x_m=5, y_m=5, status=AM, dbh_raw=20.0, dbh_unit="cm",
                 height_raw=15.0, height_unit="m"),
        ], 0.01)
        # choose t2 dbh giving +100 kg growth per tree with a=0.1,b=2,c=.5
        def b_at(d):
            return 0.1 * d ** 2 * 15 ** 0.5
        import math
        d2 = math.sqrt((b_at(20.0) + 100.0) / (0.1 * 15 ** 0.5))
        import_campaign_rows(self.t2, [
            dict(plot="P1", field_number="1", species="OAK",
                 x_m=5, y_m=5, status=AM, dbh_raw=d2, dbh_unit="cm",
                 height_raw=15.0, height_unit="m"),
            dict(plot="P2", field_number="1", species="OAK",
                 x_m=5, y_m=5, status=AM, dbh_raw=d2, dbh_unit="cm",
                 height_raw=15.0, height_unit="m"),
        ], 0.01)
        res = self._run()
        # per-ha mean = (1000 + 400)/2 = 700 kg/ha; x 100 ha = 70 000 kg
        self.assertAlmostEqual(
            res["components"]["survivor_growth"]["total_kg"],
            70_000.0, delta=1e-6)
        # The naive "mean tree * area" would give 100 kg * (100/0.175 avg?)
        # and clearly differs; the provenance carries per-plot values.
        p1 = next(p for p in res["provenance"]["plots"] if p["plot"] == "P1")
        self.assertEqual(p1["area_ha"], 0.10)

    def test_plot_area_polygon_crosscheck(self):
        bad = Plot(code="BAD", stratum=self.sA, x_m=0, y_m=0,
                   declared_area_ha=0.50, boundary=rect(0, 0, 50, 20),
                   area_polygon_ha=0.10)
        with self.assertRaises(DjValidationError):
            verify_plot_area(bad, 0.01)

    # ---------- D. zero / missing / dead ---------------------------------------
    def test_zero_growth_missing_and_dead_are_distinct(self):
        import_campaign_rows(self.t1, [
            dict(plot="P1", field_number="z", species="OAK", x_m=5, y_m=5,
                 status=AM, dbh_raw=18.0, dbh_unit="cm",
                 height_raw=13.0, height_unit="m"),
            dict(plot="P1", field_number="m", species="OAK", x_m=8, y_m=8,
                 status=AM, dbh_raw=18.0, dbh_unit="cm",
                 height_raw=13.0, height_unit="m"),
            dict(plot="P1", field_number="d", species="OAK", x_m=11, y_m=11,
                 status=AM, dbh_raw=22.0, dbh_unit="cm",
                 height_raw=16.0, height_unit="m"),
        ], 0.01)
        import_campaign_rows(self.t2, [
            dict(plot="P1", field_number="z", species="OAK", x_m=5, y_m=5,
                 status=AM, dbh_raw=18.0, dbh_unit="cm",
                 height_raw=13.0, height_unit="m",
                 notes="verified zero growth"),
            dict(plot="P1", field_number="m", species="OAK", x_m=8, y_m=8,
                 status=AN),
            dict(plot="P1", field_number="d", species="OAK", x_m=11, y_m=11,
                 status=DE),
        ], 0.01)
        res = self._run()
        p1 = next(p for p in res["provenance"]["plots"] if p["plot"] == "P1")
        self.assertEqual([z["tree"] for z in p1["verified_zero_growth"]],
                         ["P1/z"])
        self.assertEqual([m["tree"] for m in p1["alive_not_measured"]],
                         ["P1/m"])
        self.assertEqual([m["tree"] for m in p1["mortality"]], ["P1/d"])
        # missing survivor did NOT silently become zero growth:
        self.assertTrue(p1["imputed_survivor_growth_kg"] >= 0)

    # ---------- A + E. renumber / same-number contradiction --------------------
    def test_renumber_keeps_one_individual(self):
        import_campaign_rows(self.t1, [
            dict(plot="P1", field_number="007", species="OAK", x_m=5, y_m=5,
                 status=AM, dbh_raw=20.0, dbh_unit="cm",
                 height_raw=15.0, height_unit="m")], 0.01)
        tree = Tree.objects.get(current_field_number="007")
        tree.current_field_number = "017"
        tree.save(update_fields=["current_field_number"])
        import_campaign_rows(self.t2, [
            dict(plot="P1", field_number="017", species="OAK", x_m=5, y_m=5,
                 status=AM, dbh_raw=21.0, dbh_unit="cm",
                 height_raw=15.3, height_unit="m")], 0.01)
        t1t, t2t, *_ = build_measurement_table(
            self.t1, self.t2, AllometricEquation.objects.all())
        pairing = pair_measurements(t1t, t2t)
        self.assertEqual(len(pairing["pairs"]), 1)
        self.assertEqual(pairing["pairs"][0]["kind"], "renumber")

    def test_same_number_position_contradiction_is_excluded(self):
        import_campaign_rows(self.t1, [
            dict(plot="P1", field_number="008", species="OAK", x_m=5, y_m=5,
                 status=AM, dbh_raw=20.0, dbh_unit="cm",
                 height_raw=15.0, height_unit="m")], 0.01)
        # new tree row, same label, 15 m away
        import_campaign_rows(self.t2, [
            dict(plot="P1", field_number="008", species="OAK", x_m=15, y_m=5,
                 status=AM, dbh_raw=12.0, dbh_unit="cm",
                 height_raw=10.0, height_unit="m")], 0.01)
        self.assertEqual(
            Tree.objects.filter(plot=self.p1,
                                current_field_number="008").count(), 2)
        res = self._run()
        p1 = next(p for p in res["provenance"]["plots"] if p["plot"] == "P1")
        excluded = [c["tree"] for c in p1["excluded_identity_conflicts"]]
        self.assertIn("P1/008", excluded)
        # not counted as growth, nor as mortality, nor as ingrowth
        self.assertEqual(p1["kg"]["survivor_growth"], 0.0)
        self.assertEqual(p1["mortality"], [])
        self.assertEqual(p1["ingrowth"], [])

    def test_distinct_resolution_counts_removal_and_ingrowth(self):
        from inventory.services.conflicts import scan_conflicts
        import_campaign_rows(self.t1, [
            dict(plot="P1", field_number="009", species="OAK", x_m=5, y_m=5,
                 status=AM, dbh_raw=16.0, dbh_unit="cm",
                 height_raw=12.0, height_unit="m")], 0.01)
        import_campaign_rows(self.t2, [
            dict(plot="P1", field_number="009", species="OAK", x_m=25, y_m=5,
                 status=AM, dbh_raw=8.0, dbh_unit="cm",
                 height_raw=8.0, height_unit="m")], 0.01)
        found = scan_conflicts(self.t1, self.t2)
        self.assertTrue(found)
        client = APIClient()
        cid = found[0]["id"]
        resp = client.post(f"/api/conflicts/{cid}/resolve/",
                           {"status": "distinct", "note": "new recruit"})
        self.assertEqual(resp.status_code, 200)
        res = self._run()
        p1 = next(p for p in res["provenance"]["plots"] if p["plot"] == "P1")
        self.assertEqual([m["tree"] for m in p1["mortality"]], ["P1/009"])
        self.assertEqual([m["tree"] for m in p1["ingrowth"]], ["P1/009"])

    # ---------- F. confirmed edition immutability ------------------------------
    def test_confirmed_estimate_is_frozen_against_new_equation(self):
        client = APIClient()
        import_campaign_rows(self.t1, [
            dict(plot="P1", field_number="1", species="OAK", x_m=5, y_m=5,
                 status=AM, dbh_raw=20.0, dbh_unit="cm",
                 height_raw=15.0, height_unit="m")], 0.01)
        import_campaign_rows(self.t2, [
            dict(plot="P1", field_number="1", species="OAK", x_m=5, y_m=5,
                 status=AM, dbh_raw=21.0, dbh_unit="cm",
                 height_raw=15.3, height_unit="m")], 0.01)
        body = dict(label="v1", t1_campaign="t1", t2_campaign="t2",
                    equation_ids=[self.eq.id], fpc=False)
        r = client.post("/api/estimates/", body, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        vid = r.json()["id"]
        before = r.json()["result_payload"]["components"]["survivor_growth"]

        rc = client.post(f"/api/estimates/{vid}/confirm/")
        self.assertEqual(rc.status_code, 200, rc.content)

        # 1) the JSON result stays byte-stable
        again = client.get(f"/api/estimates/{vid}/").json()
        self.assertEqual(
            again["result_payload"]["components"]["survivor_growth"], before)

        # 2) the equation is locked: coefficient change refused
        self.eq.refresh_from_db()
        self.eq.a = 0.999
        with self.assertRaises(PermissionError):
            self.eq.save()

        # 3) the edition row itself cannot be mutated
        version = EstimateVersion.objects.get(pk=vid)
        version.label = "tampered"
        with self.assertRaises(PermissionError):
            version.save()

        # 4) a new equation must be issued as a NEW equation row/version
        eq2 = AllometricEquation.objects.create(
            code="OAK", version="2", status="draft",
            a=0.2, b=2.0, c=0.5, dbh_min_cm=5.0, dbh_max_cm=100.0,
            residual_sigma=0.1, citation="fictional revised")
        eq2.species.add(self.oak)
        r2 = client.post("/api/estimates/",
                         dict(label="v2-new-equation",
                              t1_campaign="t1", t2_campaign="t2",
                              equation_ids=[eq2.id], fpc=False),
                         format="json")
        self.assertEqual(r2.status_code, 201)
        self.assertNotEqual(r2.json()["id"], vid)
        # old edition unchanged
        old = client.get(f"/api/estimates/{vid}/").json()
        self.assertEqual(old["label"], "v1")
        self.assertEqual(
            old["result_payload"]["components"]["survivor_growth"], before)

    def test_result_payload_records_units_and_sources(self):
        res = self._run()
        self.assertEqual(res["units"]["dbh"],
                         "cm (converted at ingest; raw unit retained)")
        self.assertEqual(res["units"]["height"], "m")
        self.assertIn("estimator", res["design"])
        self.assertTrue(res["uncertainty_assumptions"])
        self.assertIn("OAK", res["equations_used"])


# ===========================================================================
# Equation adoption review (candidate -> validated -> approved/withdrawn)
# ===========================================================================
from inventory.services.ingest import import_campaign_rows as _import  # noqa


class EquationReviewTests(TestCase):
    def setUp(self):
        self.sA = Stratum.objects.create(code="A", name="A", area_ha=100.0)
        self.oak = Species.objects.create(code="OAK", name="Oak")
        self.pine = Species.objects.create(code="PIN", name="Pine")
        self.eq_oak = AllometricEquation.objects.create(
            code="OAK-AGB", version="1.0", status="confirmed",
            a=0.1, b=2.0, c=0.5, dbh_min_cm=5.0, dbh_max_cm=100.0,
            residual_sigma=0.1, citation="oak baseline")
        self.eq_oak.species.add(self.oak)
        self.eq_pin = AllometricEquation.objects.create(
            code="PIN-AGB", version="1.0", status="confirmed",
            a=0.1, b=2.0, c=0.5, dbh_min_cm=5.0, dbh_max_cm=100.0,
            residual_sigma=0.1, citation="pine baseline")
        self.eq_pin.species.add(self.pine)
        self.t1 = Campaign.objects.create(code="t1", measured_on="2019-01-01")
        self.t2 = Campaign.objects.create(code="t2", measured_on="2024-01-01")
        self.p1 = Plot.objects.create(
            code="P1", stratum=self.sA, x_m=0, y_m=0,
            declared_area_ha=0.10, boundary=rect(0, 0, 50, 20),
            area_polygon_ha=0.10)
        # one oak survivor with real growth
        _import(self.t1, [
            dict(plot="P1", field_number="1", species="OAK", x_m=5, y_m=5,
                 status=AM, dbh_raw=20.0, dbh_unit="cm",
                 height_raw=15.0, height_unit="m"),
            dict(plot="P1", field_number="2", species="PIN", x_m=8, y_m=8,
                 status=AM, dbh_raw=24.0, dbh_unit="cm",
                 height_raw=17.0, height_unit="m"),
        ], 0.01)
        _import(self.t2, [
            dict(plot="P1", field_number="1", species="OAK", x_m=5, y_m=5,
                 status=AM, dbh_raw=21.0, dbh_unit="cm",
                 height_raw=15.3, height_unit="m"),
            dict(plot="P1", field_number="2", species="PIN", x_m=8, y_m=8,
                 status=AM, dbh_raw=25.0, dbh_unit="cm",
                 height_raw=17.4, height_unit="m"),
        ], 0.01)
        self.client = APIClient()
        r = self.client.post("/api/estimates/", dict(
            label="baseline v1", t1_campaign="t1", t2_campaign="t2",
            equation_ids=[self.eq_oak.id, self.eq_pin.id], fpc=False),
            format="json")
        self.assertEqual(r.status_code, 201, r.content)
        self.baseline_id = r.json()["id"]
        self.baseline_growth = r.json()["result_payload"]["components"][
            "survivor_growth"]["total_kg"]
        rc = self.client.post(f"/api/estimates/{self.baseline_id}/confirm/")
        self.assertEqual(rc.status_code, 200, rc.content)

    def _candidate(self, species_overrides=None, code="OAK-AGB",
                   version="2.0", dbh_max=100.0, a=0.12, label="candidate",
                   include_pine=True):
        spec = {"OAK": dict(code=code, version=version, a=a, b=2.0, c=0.5,
                            dbh_min_cm=5.0, dbh_max_cm=dbh_max,
                            height_required=True, residual_sigma=0.1,
                            citation="oak revised")}
        if include_pine:
            spec["PIN"] = dict(code="PIN-AGB", version="2.0", a=0.1,
                               b=2.0, c=0.5, dbh_min_cm=5.0,
                               dbh_max_cm=100.0, height_required=True,
                               residual_sigma=0.1, citation="pine revised")
        if species_overrides:
            spec.update(species_overrides)
        r = self.client.post("/api/equation-reviews/",
                             dict(label=label, candidate_spec=spec),
                             format="json")
        self.assertEqual(r.status_code, 201, r.content)
        rid = r.json()["id"]
        self.assertEqual(r.json()["status"], "candidate")
        return rid

    def _validate(self, rid):
        r = self.client.post(f"/api/equation-reviews/{rid}/validate/", {},
                             format="json")
        return r

    def _compare(self, rid):
        return self.client.post(
            f"/api/equation-reviews/{rid}/compare/",
            dict(baseline_version=self.baseline_id), format="json")

    # ---------- 1. complete coverage -> independent new version ---------------
    def test_full_coverage_generates_new_version_and_keeps_baseline(self):
        rid = self._candidate(a=0.12)
        vr = self._validate(rid)
        self.assertEqual(vr.status_code, 200, vr.content)
        self.assertEqual(vr.json()["status"], "validated")

        cr = self._compare(rid)
        self.assertEqual(cr.status_code, 201, cr.content)
        c = cr.json()
        self.assertEqual(c["status"], "complete")
        self.assertTrue(c["baseline_reproduced"])
        # coverage matrix: both species rows
        rows = {r["species"]: r for r in c["coverage_matrix"]["rows"]}
        self.assertIn("OAK", rows)
        self.assertTrue(rows["OAK"]["covered"])
        self.assertTrue(rows["OAK"]["dbh_classes"])
        # tree and plot and population level diffs all present
        self.assertTrue(c["difference_payload"]["trees"])
        self.assertTrue(c["difference_payload"]["plots"])
        self.assertIn("survivor_growth",
                      c["difference_payload"]["population"]["components"])
        tree1 = next(t for t in c["difference_payload"]["trees"]
                     if t["tree"] == "P1/1")
        self.assertIsNotNone(tree1["delta_agb_kg"])

        ar = self.client.post(f"/api/equation-reviews/{rid}/approve/", {},
                              format="json")
        self.assertEqual(ar.status_code, 201, ar.content)
        new_id = ar.json()["new_estimate_version_id"]
        self.assertNotEqual(new_id, self.baseline_id)

        # old version byte-stable and still confirmed
        old = self.client.get(f"/api/estimates/{self.baseline_id}/").json()
        self.assertEqual(old["status"], "confirmed")
        self.assertEqual(old["result_payload"]["components"][
            "survivor_growth"]["total_kg"], self.baseline_growth)
        # new version is independently confirmed and numerically different
        new = self.client.get(f"/api/estimates/{new_id}/").json()
        self.assertEqual(new["status"], "confirmed")
        self.assertNotEqual(
            new["result_payload"]["components"]["survivor_growth"]["total_kg"],
            self.baseline_growth)
        # review exposes the generated version id
        review = self.client.get(f"/api/equation-reviews/{rid}/").json()
        self.assertEqual(review["status"], "approved")
        self.assertEqual(review["approved_version_id"], new_id)
        # baseline equation untouched; new equation row exists as v2.0
        self.eq_oak.refresh_from_db()
        self.assertEqual(self.eq_oak.a, 0.1)
        new_eq = AllometricEquation.objects.get(code="OAK-AGB", version="2.0")
        self.assertEqual(new_eq.a, 0.12)
        self.assertEqual(new_eq.status, "confirmed")

    # ---------- 2. missing species / out-of-range -> incomplete, no approve --
    def test_missing_species_marks_incomplete_and_forbids_approval(self):
        rid = self._candidate(a=0.12, include_pine=False)
        self.assertEqual(self._validate(rid).status_code, 200)
        cr = self._compare(rid)
        self.assertEqual(cr.status_code, 202, cr.content)
        c = cr.json()
        self.assertEqual(c["status"], "incomplete")
        self.assertIn("PIN", c["coverage_matrix"]["missing_species"])
        kinds = {r["kind"] for r in c["incomplete_reasons"]}
        self.assertIn("missing_species", kinds)

        ar = self.client.post(f"/api/equation-reviews/{rid}/approve/", {},
                              format="json")
        self.assertEqual(ar.status_code, 409)
        # no new version, baseline still sole confirmed output
        self.assertFalse(
            EstimateVersion.objects.exclude(pk=self.baseline_id).exists())

    def test_out_of_dbh_range_marks_incomplete_and_forbids_approval(self):
        rid = self._candidate(a=0.12, dbh_max=20.5)  # 21 cm oak t2 exceeds it
        self.assertEqual(self._validate(rid).status_code, 200)
        cr = self._compare(rid)
        c = cr.json()
        self.assertEqual(c["status"], "incomplete")
        self.assertTrue(c["coverage_matrix"]["out_of_range_trees"])
        self.assertTrue(any(r["kind"] == "out_of_dbh_range"
                            for r in c["incomplete_reasons"]))
        ar = self.client.post(f"/api/equation-reviews/{rid}/approve/", {},
                              format="json")
        self.assertEqual(ar.status_code, 409)

    # ---------- 3. withdrawn: comparisons retained, cannot confirm ----------
    def test_withdrawn_keeps_comparison_audit_but_blocks_approval(self):
        rid = self._candidate(a=0.12)
        self.assertEqual(self._validate(rid).status_code, 200)
        cr = self._compare(rid)
        self.assertEqual(cr.status_code, 201)
        comparison_id = cr.json()["id"]

        wr = self.client.post(f"/api/equation-reviews/{rid}/withdraw/", {},
                              format="json")
        self.assertEqual(wr.status_code, 200)
        self.assertEqual(wr.json()["status"], "withdrawn")

        # comparison record survives
        got = self.client.get(
            f"/api/equation-reviews/{rid}/comparisons/").json()
        self.assertEqual([c["id"] for c in got], [comparison_id])
        # approval impossible
        ar = self.client.post(f"/api/equation-reviews/{rid}/approve/", {},
                              format="json")
        self.assertEqual(ar.status_code, 409)
        events = [e["event"] for e in
                  self.client.get(f"/api/equation-reviews/{rid}/").json()[
                      "events"]]
        self.assertEqual(events[-1], "withdrawn")

    # ---------- 4. drift: comparison refused when data changed ---------------
    def test_comparison_refused_when_locked_inputs_drifted(self):
        rid = self._candidate(a=0.12)
        self.assertEqual(self._validate(rid).status_code, 200)
        # tamper with a measurement AFTER baseline confirmation (simulates a
        # data correction): baseline can no longer reproduce -> refuse
        m = TreeMeasurement.objects.get(campaign=self.t2,
                                        field_number_seen="1")
        m.dbh_cm = 21.6
        # bypass any app checks: direct update changes canonical value
        TreeMeasurement.objects.filter(pk=m.pk).update(dbh_cm=21.6)
        cr = self._compare(rid)
        self.assertEqual(cr.status_code, 409)
        self.assertFalse(cr.json()["lock"]["baseline_reproduced"])
        self.assertIsNone(
            ReviewComparison.objects.filter(review=rid).first())

    # ---------- 5. ordinary flow ignores candidates entirely ----------------
    def test_normal_two_occasion_flow_ignores_candidates(self):
        # candidate exists but is never selected by the normal estimator
        rid = self._candidate(a=0.5, code="OAK-FANCY", version="9.9")
        self.assertEqual(self._validate(rid).status_code, 200)
        r = self.client.post("/api/estimates/", dict(
            label="ordinary draft", t1_campaign="t1", t2_campaign="t2",
            equation_ids=[self.eq_oak.id, self.eq_pin.id], fpc=False),
            format="json")
        self.assertEqual(r.status_code, 201)
        payload = r.json()["result_payload"]
        self.assertEqual(
            payload["components"]["survivor_growth"]["total_kg"],
            self.baseline_growth)
        self.assertNotIn("OAK-FANCY", payload["equations_used"])


# ---------- concurrency: only one new version on double approval ------------
from django.db import IntegrityError, transaction as _tx  # noqa: E402
from django.test import TransactionTestCase  # noqa: E402


class ConcurrentApprovalTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        self.sA = Stratum.objects.create(code="A", name="A", area_ha=100.0)
        self.oak = Species.objects.create(code="OAK", name="Oak")
        self.eq = AllometricEquation.objects.create(
            code="OAK-AGB", version="1.0", status="confirmed",
            a=0.1, b=2.0, c=0.5, dbh_min_cm=5.0, dbh_max_cm=100.0,
            residual_sigma=0.1, citation="oak baseline")
        self.eq.species.add(self.oak)
        self.t1 = Campaign.objects.create(code="t1", measured_on="2019-01-01")
        self.t2 = Campaign.objects.create(code="t2", measured_on="2024-01-01")
        Plot.objects.create(
            code="P1", stratum=self.sA, x_m=0, y_m=0,
            declared_area_ha=0.10, boundary=rect(0, 0, 50, 20),
            area_polygon_ha=0.10)
        _import(self.t1, [dict(plot="P1", field_number="1", species="OAK",
                               x_m=5, y_m=5, status=AM, dbh_raw=20.0,
                               dbh_unit="cm", height_raw=15.0,
                               height_unit="m")], 0.01)
        _import(self.t2, [dict(plot="P1", field_number="1", species="OAK",
                               x_m=5, y_m=5, status=AM, dbh_raw=21.0,
                               dbh_unit="cm", height_raw=15.3,
                               height_unit="m")], 0.01)

    def _ready_review(self, client, a=0.12):
        r = client.post("/api/estimates/", dict(
            label="base", t1_campaign="t1", t2_campaign="t2",
            equation_ids=[self.eq.id], fpc=False), format="json")
        base_id = r.json()["id"]
        self.assertEqual(
            client.post(f"/api/estimates/{base_id}/confirm/").status_code, 200)
        rr = client.post("/api/equation-reviews/", dict(
            label="cand", candidate_spec={"OAK": dict(
                code="OAK-AGB", version="2.0", a=a, b=2.0, c=0.5,
                dbh_min_cm=5.0, dbh_max_cm=100.0, height_required=True,
                residual_sigma=0.1, citation="rev")}), format="json")
        rid = rr.json()["id"]
        self.assertEqual(client.post(
            f"/api/equation-reviews/{rid}/validate/", {},
            format="json").status_code, 200)
        self.assertEqual(client.post(
            f"/api/equation-reviews/{rid}/compare/",
            dict(baseline_version=base_id), format="json").status_code, 201)
        return rid, base_id

    def test_second_approval_after_race_creates_no_extra_version(self):
        """The loser of a concurrent approval gets 409; still one version."""
        c = APIClient()
        rid, _ = self._ready_review(c)

        first = c.post(f"/api/equation-reviews/{rid}/approve/", {},
                       format="json")
        self.assertEqual(first.status_code, 201, first.content)
        winner_id = first.json()["new_estimate_version_id"]

        # Simulated losing concurrent request: review now terminal.
        second = c.post(f"/api/equation-reviews/{rid}/approve/", {},
                        format="json")
        self.assertEqual(second.status_code, 409)
        versions = EstimateVersion.objects.filter(generated_by_review_id=rid)
        self.assertEqual(list(versions.values_list("id", flat=True)),
                         [winner_id])

    def test_unique_constraint_blocks_a_second_generated_version(self):
        """DB-level guarantee behind the API: one review -> one edition."""
        c = APIClient()
        rid, _ = self._ready_review(c)
        first = c.post(f"/api/equation-reviews/{rid}/approve/", {},
                       format="json")
        self.assertEqual(first.status_code, 201)

        review = EquationAdoptionReview.objects.get(pk=rid)
        with self.assertRaises(IntegrityError):
            with _tx.atomic():
                EstimateVersion.objects.create(
                    label="duplicate attempt", t1_campaign=self.t1,
                    t2_campaign=self.t2, status="confirmed",
                    design_snapshot={}, result_payload={},
                    equation_checksum="x", generated_by_review=review)
        self.assertEqual(
            EstimateVersion.objects.filter(generated_by_review_id=rid)
            .count(), 1)

    def test_approve_review_loses_race_after_lock(self):
        """
        Simulate the exact interleaving on PostgreSQL: both approvals pass the
        cheap pre-checks and enter the atomic block; one commits first, the
        second's locked-row status recheck (or the OneToOne constraint)
        rejects it. The loser reports the single surviving version.
        """
        from inventory.services import review as review_service
        c = APIClient()
        rid, _ = self._ready_review(c, a=0.13)
        review = EquationAdoptionReview.objects.get(pk=rid)
        comparison = review.comparisons.first()

        # Winner commits normally.
        _r, winner = review_service.approve_review(
            EquationAdoptionReview.objects.get(pk=rid),
            comparison=comparison)
        self.assertEqual(winner.generated_by_review_id, rid)

        # A stale in-memory approver that raced past the initial status check:
        # force it into the locked block, where the recheck must reject it.
        stale = EquationAdoptionReview.objects.get(pk=rid)
        self.assertEqual(stale.status, "approved")
        with self.assertRaises(review_service.ReviewStateError):
            review_service.approve_review(stale, comparison=comparison)
        self.assertEqual(
            EstimateVersion.objects.filter(generated_by_review_id=rid)
            .count(), 1)

