"""
Acceptance tests for the EQUATION ADOPTION REVIEW scenario.

The station requires proof that a new allometric-equation edition differs
from a confirmed estimate because of THE EQUATION ONLY — not because of
edited survey data, a changed identity decision, or sampling-frame drift.

Acceptance coverage:
  1. a fully covering new equation produces an independent NEW confirmed
     EstimateVersion while the old version is byte-identical/untouched;
  2. a missing species or a dbh outside the candidate's range marks the
     comparison "incomplete" and forbids approval;
  3. withdrawing a candidate keeps the existing comparison as audit but it
     can never be confirmed;
  4. concurrent approval of one review generates exactly one new version;
  5. the ordinary two-occasion estimate flow, when no candidate equation is
     chosen, behaves exactly as before.

Plus: locked frame / identity / design immutability, candidate coefficient
freeze after validation, write-once comparison and baseline reproduction.
"""
import threading

from django.db import close_old_connections, connection
from django.test import TransactionTestCase
from rest_framework.test import APIClient

from inventory.models import (
    AllometricEquation,
    CANDIDATE_APPROVED,
    CANDIDATE_VALIDATED,
    CANDIDATE_WITHDRAWN,
    EstimateVersion,
    ReviewApprovalSlot,
    REVIEW_APPROVED,
    REVIEW_WITHDRAWN,
    VERSION_CONFIRMED,
)
from inventory.services import adoption
from inventory.services.ingest import import_campaign_rows

AM, AN, DE, MI = ("alive_measured", "alive_not_measured", "dead",
                  "missing_tree")


class AdoptionFixtureMixin:
    """Two-plot stratified frame: OAK/PIN/BIR, growth, death, ingrowth."""

    def _build_frame(self):
        from inventory.models import Campaign, Plot, Species, Stratum
        sA = Stratum.objects.create(code="A", name="A", area_ha=100.0)
        self.oak = Species.objects.create(code="OAK", name="Oak")
        self.pin = Species.objects.create(code="PIN", name="Pine")
        self.bir = Species.objects.create(code="BIR", name="Birch")

        def mk_eq(code, sp, a):
            e = AllometricEquation.objects.create(
                code=code, version="1", status="confirmed",
                a=a, b=2.0, c=0.5,
                dbh_min_cm=5.0, dbh_max_cm=200.0,
                residual_sigma=0.1, citation="baseline locked eq")
            e.species.add(sp)
            return e
        self.eq_oak = mk_eq("OAK-AGB", self.oak, 0.10)
        self.eq_pin = mk_eq("PIN-AGB", self.pin, 0.09)
        self.eq_bir = mk_eq("BIR-AGB", self.bir, 0.11)

        self.t1 = Campaign.objects.create(code="t1", measured_on="2019-01-01")
        self.t2 = Campaign.objects.create(code="t2", measured_on="2024-01-01")
        Plot.objects.create(
            code="P1", stratum=sA, x_m=0, y_m=0,
            declared_area_ha=0.50,
            boundary=[[0, 0], [50, 0], [50, 100], [0, 100], [0, 0]],
            area_polygon_ha=0.50)
        Plot.objects.create(
            code="P2", stratum=sA, x_m=0, y_m=0,
            declared_area_ha=0.25,
            boundary=[[0, 0], [50, 0], [50, 50], [0, 50], [0, 0]],
            area_polygon_ha=0.25)

        import_campaign_rows(self.t1, [
            # survivors (oak/pine)
            dict(plot="P1", field_number="o1", species="OAK", x_m=5, y_m=5,
                 status=AM, dbh_raw=20.0, dbh_unit="cm",
                 height_raw=15.0, height_unit="m"),
            dict(plot="P1", field_number="o2", species="OAK", x_m=8, y_m=8,
                 status=AM, dbh_raw=30.0, dbh_unit="cm",
                 height_raw=19.0, height_unit="m"),
            dict(plot="P1", field_number="p1", species="PIN", x_m=11, y_m=11,
                 status=AM, dbh_raw=28.0, dbh_unit="cm",
                 height_raw=18.0, height_unit="m"),
            # mortality
            dict(plot="P1", field_number="d1", species="OAK", x_m=14, y_m=14,
                 status=AM, dbh_raw=22.0, dbh_unit="cm",
                 height_raw=16.0, height_unit="m"),
            dict(plot="P2", field_number="o3", species="OAK", x_m=5, y_m=5,
                 status=AM, dbh_raw=18.0, dbh_unit="cm",
                 height_raw=13.0, height_unit="m"),
        ], 0.01)
        import_campaign_rows(self.t2, [
            dict(plot="P1", field_number="o1", species="OAK", x_m=5, y_m=5,
                 status=AM, dbh_raw=21.0, dbh_unit="cm",
                 height_raw=15.4, height_unit="m"),
            dict(plot="P1", field_number="o2", species="OAK", x_m=8, y_m=8,
                 status=AM, dbh_raw=31.0, dbh_unit="cm",
                 height_raw=19.4, height_unit="m"),
            dict(plot="P1", field_number="p1", species="PIN", x_m=11, y_m=11,
                 status=AM, dbh_raw=29.0, dbh_unit="cm",
                 height_raw=18.4, height_unit="m"),
            dict(plot="P1", field_number="d1", species="OAK", x_m=14, y_m=14,
                 status=DE),
            # ingrowth birch above recruitment
            dict(plot="P2", field_number="b1", species="BIR", x_m=9, y_m=9,
                 status=AM, dbh_raw=7.0, dbh_unit="cm",
                 height_raw=7.0, height_unit="m"),
            dict(plot="P2", field_number="o3", species="OAK", x_m=5, y_m=5,
                 status=AM, dbh_raw=19.0, dbh_unit="cm",
                 height_raw=13.4, height_unit="m"),
        ], 0.01)

    def _confirm_baseline(self, label="baseline"):
        client = APIClient()
        body = dict(label=label, t1_campaign="t1", t2_campaign="t2",
                    equation_ids=[self.eq_oak.id, self.eq_pin.id,
                                  self.eq_bir.id], fpc=False)
        r = client.post("/api/estimates/", body, format="json")
        assert r.status_code == 201, r.content
        vid = r.json()["id"]
        payload_before = r.json()["result_payload"]
        rc = client.post(f"/api/estimates/{vid}/confirm/")
        assert rc.status_code == 200, rc.content
        return EstimateVersion.objects.get(pk=vid), payload_before

    def _create_candidate(self, code="OAK-AGB", version="2",
                          species=("OAK", "PIN", "BIR"), a=0.12,
                          dbh_max=200.0, citation="new edition 2026"):
        return adoption.create_candidate({
            "code": code, "version": version,
            "species_codes": list(species),
            "a": a, "b": 2.05, "c": 0.55,
            "dbh_min_cm": 5.0, "dbh_max_cm": dbh_max,
            "height_required": True, "residual_sigma": 0.12,
            "citation": citation,
        })

    def _validated_review(self, baseline, **kw):
        candidate = self._create_candidate(**kw)
        adoption.validate_candidate(candidate)
        review = adoption.create_review(candidate, baseline)
        return candidate, adoption.run_comparison(review)


class EquationAdoptionAcceptanceTests(AdoptionFixtureMixin, TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        self._build_frame()
        self.baseline, self.baseline_payload = self._confirm_baseline()
        self.client = APIClient()

    # ---------------------------------------------------- 1. complete -> new
    def test_complete_candidate_generates_independent_new_version(self):
        n_versions = EstimateVersion.objects.count()
        candidate = self._create_candidate()
        adoption.validate_candidate(candidate)
        self.assertEqual(candidate.status, CANDIDATE_VALIDATED)

        review = adoption.create_review(candidate, self.baseline)
        review = adoption.run_comparison(review)
        self.assertEqual(review.coverage_status, "complete")
        comp = review.comparison

        # baseline rerun on the LOCKED frame reproduces the frozen numbers
        self.assertTrue(comp["diff_sources"]["equation_only"])
        self.assertTrue(
            comp["diff_sources"]["baseline_reproduction"]["passed"])

        # every species and every quantified stem covered; no extrapolation
        self.assertEqual(comp["coverage"]["missing_species"], [])
        self.assertEqual(comp["coverage"]["n_out_of_range"], 0)
        self.assertEqual(comp["coverage"]["incomplete_reasons"], [])
        matrix = comp["coverage"]["matrix"]
        for sp, entry in matrix.items():
            self.assertTrue(entry["species_covered"], sp)
            for cell in entry["bins"]:
                self.assertEqual(cell["n"], cell["covered"])
                self.assertEqual(cell["missing_species"], 0)

        # per-tree: o1/o2/o3/p1 survivors quantified under both; new numbers
        o1 = next(r for r in comp["per_tree"] if r["tag"] == "P1/o1"
                  and r["role"] == "survivor_t2")
        self.assertIsNotNone(o1["agb_baseline_kg"])
        self.assertIsNotNone(o1["agb_candidate_kg"])
        self.assertNotEqual(o1["agb_baseline_kg"], o1["agb_candidate_kg"])
        # population net change differs and is carried at all three levels
        self.assertNotEqual(
            comp["population"]["net_change"]["baseline_mg"],
            comp["population"]["net_change"]["candidate_mg"])
        p1 = next(p for p in comp["per_plot"] if p["plot"] == "P1")
        self.assertNotEqual(
            p1["components"]["survivor_growth"]["baseline_kg"],
            p1["components"]["survivor_growth"]["candidate_kg"])

        review, version, equation = adoption.approve_review(review)
        candidate.refresh_from_db()

        # exactly one new edition, independently stored and confirmed
        self.assertEqual(EstimateVersion.objects.count(), n_versions + 1)
        self.assertEqual(version.status, VERSION_CONFIRMED)
        self.assertNotEqual(version.id, self.baseline.id)
        self.assertEqual(version.generated_by_review_id, review.id)
        self.assertEqual(review.status, REVIEW_APPROVED)
        self.assertEqual(candidate.status, CANDIDATE_APPROVED)
        self.assertEqual(equation.code, "OAK-AGB")
        self.assertEqual(equation.version, "2")

        # the NEW version reports adoption provenance + the same frame
        adopted = version.result_payload["adoption"]
        self.assertEqual(adopted["baseline_version_id"], self.baseline.id)
        self.assertEqual(adopted["baseline_lock_checksum"],
                         review.lock_checksum)
        # its numbers equal the comparison's candidate run exactly
        self.assertAlmostEqual(
            version.result_payload["net_change"]["total_kg"],
            comp["population"]["net_change"]["candidate_mg"] * 1000.0,
            places=6)

        # OLD version unchanged: label, checksum, result payload
        self.baseline.refresh_from_db()
        self.assertEqual(self.baseline.label, "baseline")
        self.assertEqual(
            self.baseline.result_payload["components"],
            self.baseline_payload["components"])
        self.assertNotIn("adoption", self.baseline.result_payload)
        # baseline equations untouched; candidate equation is a separate row
        self.assertEqual(AllometricEquation.objects.filter(
            code="OAK-AGB", version="1").count(), 1)
        self.assertEqual(AllometricEquation.objects.filter(
            code="OAK-AGB", version="2").count(), 1)
        self.eq_oak.refresh_from_db()
        self.assertEqual(self.eq_oak.a, 0.10)
        # audit events recorded
        events = [e.event for e in review.events.all()]
        self.assertEqual(events, ["created", "compared", "approved"])

    # --------------------------------------------- 2a. missing species blocks
    def test_missing_species_marks_incomplete_and_blocks_approval(self):
        candidate = self._create_candidate(
            code="OAK-AGB", version="2b", species=("OAK", "PIN"))
        adoption.validate_candidate(candidate)
        review = adoption.create_review(candidate, self.baseline)
        review = adoption.run_comparison(review)
        self.assertEqual(review.coverage_status, "incomplete")
        cov = review.comparison["coverage"]
        self.assertIn("BIR", cov["missing_species"])
        self.assertIn("BIR", cov["inherited_baseline_species"])
        self.assertTrue(any("missing_species" in r
                            for r in cov["incomplete_reasons"]))
        with self.assertRaises(adoption.AdoptionError) as ctx:
            adoption.approve_review(review)
        self.assertEqual(ctx.exception.http_status, 409)
        # no new edition / equation created
        self.assertEqual(EstimateVersion.objects.filter(
            generated_by_review=review).count(), 0)
        # API surface returns the same 409
        resp = self.client.post(f"/api/equation-reviews/{review.id}/approve/",
                                {}, format="json")
        self.assertEqual(resp.status_code, 409)

    # ------------------------------------------- 2b. dbh outside range blocks
    def test_dbh_outside_range_marks_incomplete_and_blocks_approval(self):
        # range tops out at 25 cm: o2 at 30/31 and p1 at 28/29 exceed it
        candidate = self._create_candidate(
            code="OAK-AGB", version="2n",
            species=("OAK", "PIN", "BIR"), dbh_max=25.0)
        adoption.validate_candidate(candidate)
        review = adoption.create_review(candidate, self.baseline)
        review = adoption.run_comparison(review)
        self.assertEqual(review.coverage_status, "incomplete")
        tags = {r["tag"] for r in review.comparison["coverage"]["out_of_range"]}
        self.assertIn("P1/o2", tags)
        self.assertIn("P1/p1", tags)
        self.assertTrue(any("dbh_outside_range" in r
                            for r in review.comparison["coverage"]
                            ["incomplete_reasons"]))
        # the comparison still QUANTIFIES and shows the extrapolation — it
        # is visible, not hidden, but approval stays forbidden
        with self.assertRaises(adoption.AdoptionError):
            adoption.approve_review(review)

    # ------------------------------------------------- 3. withdrawal is final
    def test_withdrawn_candidate_keeps_comparison_but_cannot_confirm(self):
        candidate, review = self._validated_review(self.baseline)
        frozen = review.comparison
        candidate, reviews = adoption.withdraw_candidate(
            candidate, note="literature retracted")
        review.refresh_from_db()
        self.assertEqual(candidate.status, CANDIDATE_WITHDRAWN)
        self.assertEqual(review.status, REVIEW_WITHDRAWN)
        # comparison payload retained for audit
        self.assertIsNotNone(review.comparison)
        self.assertEqual(review.comparison, frozen)
        self.assertTrue(any(
            e.event == "withdrawn" for e in review.events.all()))
        # cannot approve, cannot re-compare, cannot mutate
        with self.assertRaises(adoption.AdoptionError) as ctx:
            adoption.approve_review(review)
        self.assertEqual(ctx.exception.http_status, 409)
        with self.assertRaises(adoption.AdoptionError):
            adoption.run_comparison(review)
        # withdrawing again is a no-go
        with self.assertRaises(adoption.AdoptionError):
            adoption.withdraw_candidate(candidate)
        # no version was produced
        self.assertFalse(EstimateVersion.objects.filter(
            generated_by_review=review).exists())

    # --------------------------------------------------- 4. concurrent approve
    def test_concurrent_approval_generates_one_version(self):
        candidate, review = self._validated_review(self.baseline)
        review_id = review.id
        baseline_count = EstimateVersion.objects.count()

        barrier = threading.Barrier(2)
        results = []

        def approve():
            close_old_connections()
            try:
                client = APIClient()
                barrier.wait(timeout=10)
                resp = client.post(
                    f"/api/equation-reviews/{review_id}/approve/",
                    {}, format="json")
                results.append(resp.status_code)
            finally:
                connection.close()

        t1 = threading.Thread(target=approve)
        t2 = threading.Thread(target=approve)
        t1.start(); t2.start()
        t1.join(30); t2.join(30)
        self.assertEqual(sorted(results), [201, 409])
        self.assertEqual(EstimateVersion.objects.count(), baseline_count + 1)
        self.assertEqual(ReviewApprovalSlot.objects.filter(
            review_id=review_id).count(), 1)
        review.refresh_from_db()
        self.assertEqual(review.status, REVIEW_APPROVED)
        self.assertEqual(review.generated_versions.count(), 1)

    # ---------------------------------------- 5. ordinary flow ignores reviews
    def test_ordinary_flow_unchanged_when_candidate_not_selected(self):
        # an existing candidate/review must not leak into /api/estimates/
        self._validated_review(self.baseline)
        body = dict(label="ordinary rerun", t1_campaign="t1",
                    t2_campaign="t2",
                    equation_ids=[self.eq_oak.id, self.eq_pin.id,
                                  self.eq_bir.id], fpc=False)
        r = self.client.post("/api/estimates/", body, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        data = r.json()
        self.assertEqual(data["status"], "draft")
        self.assertIsNone(data["generated_by_review"])
        self.assertEqual(
            data["result_payload"]["components"],
            self.baseline_payload["components"])
        # candidate endpoints do not list candidates as usable equations
        eqs = {e["code"] + "@" + e["version"]
               for e in self.client.get("/api/equations/").json()}
        self.assertIn("OAK-AGB@1", eqs)
        self.assertNotIn("OAK-AGB@2", eqs)

    # ---------------------------------------------- extras: locks & immutability
    def test_comparison_uses_locked_identity_and_design(self):
        candidate = self._create_candidate(code="OAK-AGB", version="2l")
        adoption.validate_candidate(candidate)
        review = adoption.create_review(candidate, self.baseline)
        lock = review.lock_snapshot
        # identity decisions are snapshotted (none open in this fixture)
        self.assertEqual(
            {c["status"] for c in lock["identity"]["conflicts"]}
            - {"renumber", "distinct"}, set())
        # design parameters locked
        self.assertEqual(lock["design"]["t1_code"], "t1")
        self.assertEqual(lock["design"]["t2_code"], "t2")
        self.assertAlmostEqual(
            lock["design"]["recruitment_cm"], 5.0)
        self.assertEqual(sorted(lock["plots"]), ["P1", "P2"])
        # lock checksum is a sha256 hex digest and is stable
        self.assertEqual(len(review.lock_checksum), 64)
        self.assertEqual(
            adoption._snapshot_checksum(lock), review.lock_checksum)

        # editing the frame AFTER locking must not move the comparison:
        # remove a measurement row from the live DB and rerun comparison
        from inventory.models import TreeMeasurement
        TreeMeasurement.objects.filter(
            tree__plot__code="P1", campaign=self.t2,
            field_number_seen="o1").delete()
        review2 = adoption.run_comparison(review)
        # still reproduced the frozen baseline from the locked rows
        self.assertTrue(review2.comparison["diff_sources"]["equation_only"])

    def test_comparison_is_write_once(self):
        candidate, review = self._validated_review(self.baseline)
        with self.assertRaises(adoption.AdoptionError) as ctx:
            adoption.run_comparison(review)
        self.assertEqual(ctx.exception.http_status, 409)

    def test_validated_candidate_coefficients_are_frozen(self):
        candidate = self._create_candidate(code="OAK-AGB", version="2f")
        adoption.validate_candidate(candidate)
        candidate.a = 0.5
        with self.assertRaises(PermissionError):
            candidate.save()
        # species list frozen too
        from inventory.models import Species
        Species.objects.create(code="ASH", name="Ash")
        with self.assertRaises(PermissionError):
            candidate.species.add(Species.objects.get(code="ASH"))
        # review against a non-confirmed (draft) baseline is rejected
        draft = self.client.post("/api/estimates/", dict(
            label="draft x", t1_campaign="t1", t2_campaign="t2",
            equation_ids=[self.eq_oak.id, self.eq_pin.id, self.eq_bir.id],
            fpc=False), format="json").json()
        from django.shortcuts import get_object_or_404
        draft_version = get_object_or_404(EstimateVersion, pk=draft["id"])
        with self.assertRaises(adoption.AdoptionError) as ctx:
            adoption.create_review(candidate, draft_version)
        self.assertEqual(ctx.exception.http_status, 409)

    def test_candidate_codeversion_collision_rejected(self):
        # cannot shadow an existing locked equation
        with self.assertRaises(adoption.AdoptionError) as ctx:
            self._create_candidate(code="OAK-AGB", version="1")
        self.assertEqual(ctx.exception.http_status, 409)
        # duplicate candidate
        self._create_candidate(code="X", version="9")
        with self.assertRaises(adoption.AdoptionError) as ctx:
            self._create_candidate(code="X", version="9")
        self.assertEqual(ctx.exception.http_status, 409)

    def test_review_cannot_be_created_before_validation(self):
        candidate = self._create_candidate(code="Y", version="1")
        with self.assertRaises(adoption.AdoptionError) as ctx:
            adoption.create_review(candidate, self.baseline)
        self.assertEqual(ctx.exception.http_status, 409)

    def test_full_candidate_api_flow(self):
        # End-to-end HTTP coverage of the four workflow stages.
        r = self.client.post("/api/candidate-equations/", {
            "code": "OAK-AGB", "version": "2api",
            "species_codes": ["OAK", "PIN", "BIR"],
            "a": 0.12, "b": 2.05, "c": 0.55,
            "dbh_min_cm": 5.0, "dbh_max_cm": 200.0,
            "residual_sigma": 0.12, "citation": "api edition",
        }, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        cid = r.json()["id"]
        self.assertEqual(r.json()["status"], "candidate")

        rv = self.client.post(f"/api/candidate-equations/{cid}/validate/",
                              {}, format="json")
        self.assertEqual(rv.status_code, 200, rv.content)
        self.assertEqual(rv.json()["status"], "validated")
        self.assertTrue(rv.json()["validation_result"]["passed"])

        rr = self.client.post("/api/equation-reviews/", {
            "candidate_id": cid,
            "baseline_version_id": self.baseline.id,
        }, format="json")
        self.assertEqual(rr.status_code, 201, rr.content)
        rid = rr.json()["id"]
        self.assertEqual(rr.json()["coverage_status"], "pending")

        rc = self.client.post(f"/api/equation-reviews/{rid}/compare/",
                              {}, format="json")
        self.assertEqual(rc.status_code, 200, rc.content)
        self.assertEqual(rc.json()["coverage_status"], "complete")

        ra = self.client.post(f"/api/equation-reviews/{rid}/approve/",
                              {}, format="json")
        self.assertEqual(ra.status_code, 201, ra.content)
        new_vid = ra.json()["new_version"]["id"]
        self.assertEqual(ra.json()["new_version"]["status"], "confirmed")

        # history list and events endpoints
        listing = self.client.get("/api/equation-reviews/").json()
        mine = next(x for x in listing if x["id"] == rid)
        self.assertEqual(mine["status"], "approved")
        self.assertEqual(mine["new_version_id"], new_vid)
        events = self.client.get(
            f"/api/equation-reviews/{rid}/events/").json()
        self.assertEqual([e["event"] for e in events],
                         ["created", "compared", "approved"])
        # approving again -> 409, still one version
        again = self.client.post(f"/api/equation-reviews/{rid}/approve/",
                                 {}, format="json")
        self.assertEqual(again.status_code, 409)
