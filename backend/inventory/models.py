"""
Domain model for repeated-measure permanent forest plots.

Explicit design choices
=======================
* Every measured quantity carries its unit — the database never stores a
  "dbh number" without dbh_unit next to it. Accepted units: cm/mm/in for
  dbh, m for height. Unit conversion happens at ingest; canonical storage
  is centimetres (dbh) and metres (height).
* Tree coordinates are projected (x_m, y_m) in SURVEY_CRS_EPSG; plot
  boundaries are GeoJSON polygons in the same CRS. With PostGIS,
  deploy/postgis.sql adds generated geometry columns + GiST indexes.
* Individual tree identity across surveys is NOT assumed from equal tree
  numbers. Same number + contradictory position opens an IdentityConflict
  and the pair stays out of growth estimation until a human resolves it.
* Missing measurements (alive but not measured) are distinct from real
  zero growth (measured, |Δdbh| <= tolerance, cross-checked) and from
  mortality (a mortality observation exists).
* Allometric equations are versioned. EstimateVersion freezes the
  equations and a SHA-256 checksum; confirmed versions are locked and a
  new equation can never silently alter a published estimate.
"""
from django.db import models


# ---------------------------------------------------------------- constants
DBH_UNITS = ["cm", "mm", "in"]
HEIGHT_UNITS = ["m"]

DBH_TO_CM = {"cm": 1.0, "mm": 0.1, "in": 2.54}

# Tree measurement status — three mutually exclusive situations that a
# careless field database tends to collapse into "dbh = 0".
STATUS_ALIVE_MEASURED = "alive_measured"
STATUS_ALIVE_NOT_MEASURED = "alive_not_measured"   # missing, NOT zero
STATUS_DEAD = "dead"                                # mortality
STATUS_MISSING = "missing_tree"                     # not located at all
TREE_STATUS_CHOICES = [
    (STATUS_ALIVE_MEASURED, "Alive and remeasured"),
    (STATUS_ALIVE_NOT_MEASURED, "Alive but not measured (missing data)"),
    (STATUS_DEAD, "Dead (mortality observation)"),
    (STATUS_MISSING, "Tree not located"),
]

CONFLICT_OPEN = "open"
CONFLICT_RENUMBER = "renumber"          # same tree, new number
CONFLICT_DISTINCT = "distinct"          # genuinely different individuals
CONFLICT_CHOICES = [
    (CONFLICT_OPEN, "Unverified — excluded from estimates"),
    (CONFLICT_RENUMBER, "Verified renumber (same individual)"),
    (CONFLICT_DISTINCT, "Verified distinct individuals"),
]

EQUATION_DRAFT = "draft"
EQUATION_CONFIRMED = "confirmed"
EQUATION_RETIRED = "retired"
EQUATION_STATUS_CHOICES = [
    (EQUATION_DRAFT, "Draft (not usable for estimates)"),
    (EQUATION_CONFIRMED, "Confirmed (locked when used)"),
    (EQUATION_RETIRED, "Retired"),
]

VERSION_DRAFT = "draft"
VERSION_CONFIRMED = "confirmed"
VERSION_SUPERSEDED = "superseded"
VERSION_STATUS_CHOICES = [
    (VERSION_DRAFT, "Draft — recomputes with current data/equations"),
    (VERSION_CONFIRMED, "Confirmed — frozen, immutable"),
    (VERSION_SUPERSEDED, "Superseded by a newer confirmed version"),
]

# Equation adoption review lifecycle:
#   candidate  — proposal entered, coefficients/species/dbh-classes held as
#                an immutable JSON spec (NEVER an edit to a locked equation);
#   validated  — structural/domain checks passed (see validation_payload);
#   approved   — adopted: new AllometricEquation rows + a NEW EstimateVersion;
#   withdrawn  — abandoned; comparisons stay on record but approval is barred.
REVIEW_CANDIDATE = "candidate"
REVIEW_VALIDATED = "validated"
REVIEW_APPROVED = "approved"
REVIEW_WITHDRAWN = "withdrawn"
REVIEW_STATUS_CHOICES = [
    (REVIEW_CANDIDATE, "Candidate — under review"),
    (REVIEW_VALIDATED, "Validated — checks passed, awaiting decision"),
    (REVIEW_APPROVED, "Approved — new estimate version generated"),
    (REVIEW_WITHDRAWN, "Withdrawn — comparisons retained for audit"),
]

COMPARISON_COMPLETE = "complete"
COMPARISON_INCOMPLETE = "incomplete"
COMPARISON_STATUS_CHOICES = [
    (COMPARISON_COMPLETE, "Complete — candidate fully covers the baseline"),
    (COMPARISON_INCOMPLETE, "Incomplete — missing species or dbh class"),
]


class Stratum(models.Model):
    """Sampling stratum with known land area (the sampling frame)."""

    code = models.CharField(max_length=16, unique=True)
    name = models.CharField(max_length=120)
    area_ha = models.FloatField(
        help_text="Known stratum land area in hectares (sampling frame)."
    )

    class Meta:
        ordering = ["code"]

    def __str__(self):
        return f"{self.code} ({self.name})"


class Plot(models.Model):
    """A permanent sample plot. Area is per plot, not assumed constant."""

    code = models.CharField(max_length=16, unique=True)
    stratum = models.ForeignKey(
        Stratum, on_delete=models.PROTECT, related_name="plots"
    )
    # Projected coordinates of plot centre, metres.
    x_m = models.FloatField(help_text="Plot centre X, projected CRS [m].")
    y_m = models.FloatField(help_text="Plot centre Y, projected CRS [m].")
    declared_area_ha = models.FloatField(
        help_text="Declared plot area in hectares. Plots may differ in size."
    )
    # GeoJSON-like ring: [[x, y], ...] in projected metres.
    boundary = models.JSONField(
        help_text="Boundary ring [[x_m, y_m], ...] in projected CRS."
    )
    area_polygon_ha = models.FloatField(
        help_text="Polygon area in ha, computed at ingest and cross-checked."
    )

    class Meta:
        ordering = ["code"]

    def __str__(self):
        return self.code


class Species(models.Model):
    code = models.CharField(max_length=16, unique=True)
    name = models.CharField(max_length=160)
    family = models.CharField(max_length=120, blank=True)

    class Meta:
        verbose_name_plural = "species"
        ordering = ["code"]

    def __str__(self):
        return f"{self.code} — {self.name}"


class AllometricEquation(models.Model):
    """
    Versioned allometric biomass equation.

        agb_kg = a * (dbh_cm ** b) * (height_m ** c)

    Applicability is explicit (species list, dbh range, source/citation).
    Residual error is stored as a dimensionless multiplicative sigma
    (agb * exp(eps), eps ~ N(0, residual_sigma^2)) and propagated into
    equation-error components of uncertainty.
    """

    code = models.CharField(max_length=32)
    version = models.CharField(max_length=16)
    species = models.ManyToManyField(Species, related_name="equations")
    status = models.CharField(
        max_length=16, choices=EQUATION_STATUS_CHOICES, default=EQUATION_DRAFT
    )
    form = models.CharField(
        max_length=64,
        default="agb = a * dbh_cm^b * height_m^c",
        help_text="Human-readable equation form.",
    )
    a = models.FloatField()
    b = models.FloatField()
    c = models.FloatField()
    dbh_min_cm = models.FloatField()
    dbh_max_cm = models.FloatField()
    height_required = models.BooleanField(default=True)
    residual_sigma = models.FloatField(
        help_text="Multiplicative residual SD of ln(agb) [dimensionless]."
    )
    citation = models.CharField(max_length=240)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["code", "version"]
        unique_together = [("code", "version")]

    @property
    def coefficient_checksum_source(self):
        return (
            f"{self.code}|{self.version}|{self.a:.10g}|{self.b:.10g}|"
            f"{self.c:.10g}|{self.dbh_min_cm:.6g}|{self.dbh_max_cm:.6g}|"
            f"{self.residual_sigma:.6g}"
        )

    _FROZEN_FIELDS = ("code", "version", "a", "b", "c", "dbh_min_cm",
                      "dbh_max_cm", "residual_sigma", "form",
                      "height_required")

    def save(self, *args, **kwargs):
        if self.pk:
            original = type(self).objects.get(pk=self.pk)
            if original.status == EQUATION_CONFIRMED:
                changed = [
                    f for f in self._FROZEN_FIELDS
                    if getattr(original, f) != getattr(self, f)
                ]
                if changed:
                    raise PermissionError(
                        f"Equation {self.code} v{self.version} is locked by a "
                        f"confirmed estimate; changed fields {changed}. Issue "
                        "a new equation code/version instead."
                    )
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.code} v{self.version} [{self.status}]"


class Campaign(models.Model):
    """One measurement occasion (survey edition)."""

    code = models.CharField(max_length=16, unique=True)
    measured_on = models.DateField()
    description = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["measured_on"]

    def __str__(self):
        return f"{self.code} ({self.measured_on})"


class Tree(models.Model):
    """
    A tracked individual. The field number is a label, not a primary key:
    renumbers keep the same Tree row (current_field_number changes), while
    a same-number/different-location finding becomes a second Tree row plus
    an IdentityConflict until verified.
    """

    plot = models.ForeignKey(Plot, on_delete=models.PROTECT, related_name="trees")
    current_field_number = models.CharField(max_length=16)
    species = models.ForeignKey(
        Species, on_delete=models.PROTECT, related_name="trees"
    )
    first_campaign = models.ForeignKey(
        Campaign, on_delete=models.PROTECT, related_name="trees_first"
    )
    superseded_tree = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="successors",
        help_text="Set when this row re-numbers an earlier tree (verified).",
    )

    class Meta:
        # Field numbers are LABELS, not identities: a renumber keeps one
        # Tree row, a same-number/different-position finding creates two
        # Tree rows carrying the same label in one plot. The authoritative
        # label per occasion is TreeMeasurement.field_number_seen.
        indexes = [
            models.Index(fields=["plot", "current_field_number"]),
        ]
        ordering = ["plot__code", "current_field_number"]

    def __str__(self):
        return f"{self.plot.code}/{self.current_field_number}"


class TreeMeasurement(models.Model):
    """One tree measured (or sought and found dead/missing) at one campaign."""

    tree = models.ForeignKey(
        Tree, on_delete=models.PROTECT, related_name="measurements"
    )
    campaign = models.ForeignKey(
        Campaign, on_delete=models.PROTECT, related_name="measurements"
    )
    field_number_seen = models.CharField(
        max_length=16,
        help_text="Number physically on the tag at this campaign.",
    )
    x_m = models.FloatField(help_text="Stem position X, projected CRS [m].")
    y_m = models.FloatField(help_text="Stem position Y, projected CRS [m].")
    status = models.CharField(max_length=20, choices=TREE_STATUS_CHOICES)

    # Raw entry keeps the original unit; canonical *_cm / *_m columns are
    # converted at ingest. Both are retained so a unit error is auditable.
    dbh_raw = models.FloatField(null=True, blank=True)
    dbh_unit = models.CharField(max_length=2, null=True, blank=True,
                                choices=[(u, u) for u in DBH_UNITS])
    dbh_cm = models.FloatField(null=True, blank=True)

    height_raw = models.FloatField(null=True, blank=True)
    height_unit = models.CharField(max_length=2, null=True, blank=True,
                                   choices=[(u, u) for u in HEIGHT_UNITS])
    height_m = models.FloatField(null=True, blank=True)

    notes = models.CharField(max_length=240, blank=True)

    class Meta:
        unique_together = [("tree", "campaign")]
        ordering = ["tree__plot__code", "tree__current_field_number"]

    def __str__(self):
        return f"{self.tree} @ {self.campaign.code}: {self.status}"


class IdentityConflict(models.Model):
    """
    Same field number at both campaigns, but positions contradict the
    hypothesis "same individual". Never auto-merged: stays open (and the
    trees are excluded from survivor growth) until verified.
    """

    plot = models.ForeignKey(Plot, on_delete=models.PROTECT)
    field_number = models.CharField(max_length=16)
    t1_campaign = models.ForeignKey(
        Campaign, on_delete=models.PROTECT, related_name="conflicts_t1"
    )
    t2_campaign = models.ForeignKey(
        Campaign, on_delete=models.PROTECT, related_name="conflicts_t2"
    )
    t1_measurement = models.ForeignKey(
        TreeMeasurement, on_delete=models.PROTECT, related_name="conflicts_as_t1"
    )
    t2_measurement = models.ForeignKey(
        TreeMeasurement, on_delete=models.PROTECT, related_name="conflicts_as_t2"
    )
    distance_m = models.FloatField(
        help_text="Distance between the two reported positions [m]."
    )
    status = models.CharField(
        max_length=10, choices=CONFLICT_CHOICES, default=CONFLICT_OPEN
    )
    resolution_note = models.CharField(max_length=240, blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["plot__code", "field_number"]

    def __str__(self):
        return f"{self.plot.code}/{self.field_number}: {self.status}"


class MeasurementImportRow(models.Model):
    """Audit trail for raw imported rows, including rejected ones."""

    campaign = models.ForeignKey(
        Campaign, on_delete=models.PROTECT, related_name="import_rows"
    )
    plot = models.ForeignKey(
        Plot, on_delete=models.PROTECT, related_name="import_rows"
    )
    raw_payload = models.JSONField()
    accepted = models.BooleanField()
    rejection_reason = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)


class EstimateVersion(models.Model):
    """
    An immutable edition of the population estimate.

    On confirmation:
      * status -> confirmed (edits to the row are blocked in save());
      * equations referenced get locked;
      * result_payload + equation_checksum are frozen.
    A later new allometric equation creates a NEW version; the confirmed
    one can never be silently changed.
    """

    label = models.CharField(max_length=120)
    t1_campaign = models.ForeignKey(
        Campaign, on_delete=models.PROTECT, related_name="estimate_t1"
    )
    t2_campaign = models.ForeignKey(
        Campaign, on_delete=models.PROTECT, related_name="estimate_t2"
    )
    equations = models.ManyToManyField(AllometricEquation, related_name="estimates")
    status = models.CharField(
        max_length=12, choices=VERSION_STATUS_CHOICES, default=VERSION_DRAFT
    )
    design_snapshot = models.JSONField(
        help_text="Stratum areas, expansion design, thresholds, CRS, "
                  "uncertainty assumptions used for this run."
    )
    result_payload = models.JSONField(
        null=True, blank=True,
        help_text="Full component breakdown + provenance + uncertainty."
    )
    equation_checksum = models.CharField(max_length=64, blank=True)
    # Set ONLY when this edition was generated by approving an
    # EquationAdoptionReview. The one-to-one relation makes "one review ->
    # exactly one edition" a DATABASE-level guarantee, so a concurrent
    # double-approval can never mint two versions (the second transaction
    # violates the unique constraint even under row-level locking).
    generated_by_review = models.OneToOneField(
        "EquationAdoptionReview",
        on_delete=models.PROTECT,
        null=True, blank=True,
        related_name="generated_version",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    confirmed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def save(self, *args, **kwargs):
        if self.pk:
            original = type(self).objects.get(pk=self.pk)
            if original.status == VERSION_CONFIRMED:
                raise PermissionError(
                    "EstimateVersion is confirmed/frozen; create a new "
                    "version instead of mutating this one."
                )
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.label} [{self.status}]"


class EquationAdoptionReview(models.Model):
    """
    Adoption review of a *new* allometric equation set.

    The proposal is a JSON candidate spec — coefficient set, applicability
    (species + dbh class), residual sigma, citation, equation form. It is
    deliberately NOT a row of AllometricEquation: a candidate cannot be
    selected by the ordinary draft-estimate flow, and none of it can touch a
    locked (confirmed) equation. Only on approval do real equation rows get
    created (a new code/version) together with a NEW EstimateVersion.

    Lifecycle: candidate -> validated -> approved | withdrawn.
    The spec is frozen at validation; approval/withdrawal are terminal.
    """

    label = models.CharField(max_length=120)
    status = models.CharField(
        max_length=12, choices=REVIEW_STATUS_CHOICES,
        default=REVIEW_CANDIDATE,
    )
    candidate_spec = models.JSONField(
        help_text="Candidate equation set: {species_code: {code, version, "
                  "a, b, c, dbh_min_cm, dbh_max_cm, height_required, "
                  "residual_sigma, citation, form}, ...}."
    )
    species_scope = models.JSONField(
        default=list,
        help_text="Applicable species codes covered by the candidate.",
    )
    validation_payload = models.JSONField(
        null=True, blank=True,
        help_text="Structural/domain validation outcome (checks + coverage "
                  "preview at validation time)."
    )
    validated_at = models.DateTimeField(null=True, blank=True)
    approved_at = models.DateTimeField(null=True, blank=True)
    withdrawn_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    _FROZEN_AFTER_VALIDATION = ("candidate_spec", "species_scope")
    _TERMINAL_STATUSES = (REVIEW_APPROVED, REVIEW_WITHDRAWN)

    def save(self, *args, **kwargs):
        if self.pk:
            original = type(self).objects.get(pk=self.pk)
            if original.status in self._TERMINAL_STATUSES:
                raise PermissionError(
                    f"Equation adoption review is {original.status}; it is "
                    "terminal and cannot be modified."
                )
            if original.status == REVIEW_VALIDATED:
                changed = [
                    f for f in self._FROZEN_AFTER_VALIDATION
                    if getattr(original, f) != getattr(self, f)
                ]
                if changed:
                    raise PermissionError(
                        "Candidate spec is frozen once validated; withdraw "
                        f"and open a new review instead (fields {changed})."
                    )
        super().save(*args, **kwargs)

    def __str__(self):
        return f"Review {self.label} [{self.status}]"


class ReviewEvent(models.Model):
    """
    Immutable audit record of every review action: created / validated /
    compared / approval attempted / approved / withdrawn. Comparisons survive
    withdrawal here and on ReviewComparison so the audit trail is complete.
    """

    EVENT_CREATED = "created"
    EVENT_VALIDATED = "validated"
    EVENT_COMPARED = "compared"
    EVENT_APPROVAL_REJECTED = "approval_rejected"
    EVENT_APPROVED = "approved"
    EVENT_WITHDRAWN = "withdrawn"
    EVENT_CHOICES = [
        (EVENT_CREATED, "Candidate created"),
        (EVENT_VALIDATED, "Validation run"),
        (EVENT_COMPARED, "Impact comparison against a locked baseline"),
        (EVENT_APPROVAL_REJECTED, "Approval attempted and rejected"),
        (EVENT_APPROVED, "Approved — new equation rows + estimate version"),
        (EVENT_WITHDRAWN, "Withdrawn"),
    ]

    review = models.ForeignKey(
        EquationAdoptionReview, on_delete=models.PROTECT,
        related_name="events",
    )
    event = models.CharField(max_length=20, choices=EVENT_CHOICES)
    actor = models.CharField(max_length=80, blank=True,
                             help_text="Reviewer / system actor.")
    note = models.CharField(max_length=400, blank=True)
    payload = models.JSONField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]


class ReviewComparison(models.Model):
    """
    A locked impact comparison of a candidate against ONE confirmed
    EstimateVersion baseline.

    The baseline's survey data, human identity decisions and sampling-design
    snapshot are re-read, fingerprinted and re-estimated with the SAME
    baseline equations; only if that reproduces the confirmed payload is the
    candidate run accepted. Any fingerprint/result mismatch means data,
    identity or frame drift and the comparison is refused.

    Neither the baseline version nor its equations/result are modified.
    A comparison is retained for audit even if the review is withdrawn.
    """

    review = models.ForeignKey(
        EquationAdoptionReview, on_delete=models.PROTECT,
        related_name="comparisons",
    )
    baseline_version = models.ForeignKey(
        EstimateVersion, on_delete=models.PROTECT,
        related_name="candidate_comparisons",
    )
    status = models.CharField(
        max_length=12, choices=COMPARISON_STATUS_CHOICES,
        default=COMPARISON_INCOMPLETE,
    )
    # Fingerprints of the locked inputs, so drift can be re-detected later
    # (e.g. at approval time).
    lock_fingerprints = models.JSONField(
        help_text="measurements / identity / frame / baseline-result hashes "
                  "captured when the baseline was locked."
    )
    coverage_matrix = models.JSONField(
        help_text="Per-species x dbh-class coverage: covered, missing "
                  "species, out-of-range counts."
    )
    difference_payload = models.JSONField(
        help_text="Tree-level, plot-level and population-level baseline vs "
                  "candidate results, extrapolation and missing coverage."
    )
    incomplete_reasons = models.JSONField(
        default=list,
        help_text="Human-readable reasons the comparison is not approvable.",
    )
    baseline_reproduced = models.BooleanField(
        help_text="Re-running the baseline equations reproduced the "
                  "confirmed result byte-for-byte (no equation-independent "
                  "drift)."
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
