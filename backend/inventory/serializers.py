from rest_framework import serializers

from inventory.models import (
    AllometricEquation,
    Campaign,
    EquationAdoptionReview,
    EstimateVersion,
    IdentityConflict,
    Plot,
    ReviewComparison,
    ReviewEvent,
    Species,
    Stratum,
    Tree,
    TreeMeasurement,
)


class StratumSerializer(serializers.ModelSerializer):
    class Meta:
        model = Stratum
        fields = ["id", "code", "name", "area_ha"]


class SpeciesSerializer(serializers.ModelSerializer):
    class Meta:
        model = Species
        fields = ["id", "code", "name", "family"]


class CampaignSerializer(serializers.ModelSerializer):
    class Meta:
        model = Campaign
        fields = ["id", "code", "measured_on", "description"]


class PlotSerializer(serializers.ModelSerializer):
    stratum_code = serializers.CharField(source="stratum.code", read_only=True)
    stratum_name = serializers.CharField(source="stratum.name", read_only=True)
    crs_epsg = serializers.SerializerMethodField()

    class Meta:
        model = Plot
        fields = [
            "id", "code", "stratum", "stratum_code", "stratum_name",
            "x_m", "y_m", "declared_area_ha", "area_polygon_ha",
            "boundary", "crs_epsg",
        ]

    def get_crs_epsg(self, _obj):
        from django.conf import settings
        return settings.SURVEY_CRS_EPSG


class EquationSerializer(serializers.ModelSerializer):
    species_codes = serializers.SlugRelatedField(
        many=True, read_only=True, slug_field="code", source="species"
    )

    class Meta:
        model = AllometricEquation
        fields = [
            "id", "code", "version", "species_codes", "status", "form",
            "a", "b", "c", "dbh_min_cm", "dbh_max_cm",
            "height_required", "residual_sigma", "citation", "created_at",
        ]


class TreeSerializer(serializers.ModelSerializer):
    plot_code = serializers.CharField(source="plot.code", read_only=True)
    species_code = serializers.CharField(source="species.code", read_only=True)
    supersedes = serializers.PrimaryKeyRelatedField(
        source="superseded_tree", read_only=True
    )

    class Meta:
        model = Tree
        fields = [
            "id", "plot", "plot_code", "species_code",
            "current_field_number", "first_campaign", "supersedes",
        ]


class MeasurementSerializer(serializers.ModelSerializer):
    plot_code = serializers.CharField(source="tree.plot.code", read_only=True)
    field_number = serializers.CharField(source="field_number_seen")

    class Meta:
        model = TreeMeasurement
        fields = [
            "id", "tree", "campaign", "plot_code", "field_number",
            "x_m", "y_m", "status",
            "dbh_raw", "dbh_unit", "dbh_cm",
            "height_raw", "height_unit", "height_m", "notes",
        ]


class ConflictSerializer(serializers.ModelSerializer):
    class Meta:
        model = IdentityConflict
        fields = [
            "id", "plot", "field_number", "t1_campaign", "t2_campaign",
            "t1_measurement", "t2_measurement", "distance_m",
            "status", "resolution_note", "resolved_at",
        ]
        read_only_fields = ["distance_m", "resolved_at"]


class ConflictResolveSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=["renumber", "distinct"])
    note = serializers.CharField(required=False, allow_blank=True)


class EstimateVersionSerializer(serializers.ModelSerializer):
    class Meta:
        model = EstimateVersion
        fields = [
            "id", "label", "t1_campaign", "t2_campaign", "status",
            "design_snapshot", "result_payload", "equation_checksum",
            "created_at", "confirmed_at",
        ]
        read_only_fields = [
            "status", "design_snapshot", "result_payload",
            "equation_checksum", "confirmed_at",
        ]


class MeasurementImportRowSerializer(serializers.Serializer):
    """One raw field row. Units are mandatory with every value."""

    plot = serializers.CharField()
    field_number = serializers.CharField()
    species = serializers.CharField()
    x_m = serializers.FloatField()
    y_m = serializers.FloatField()
    status = serializers.ChoiceField(
        choices=["alive_measured", "alive_not_measured", "dead", "missing_tree"]
    )
    dbh_raw = serializers.FloatField(required=False, allow_null=True)
    dbh_unit = serializers.ChoiceField(choices=["cm", "mm", "in"],
                                       required=False, allow_null=True)
    height_raw = serializers.FloatField(required=False, allow_null=True)
    height_unit = serializers.ChoiceField(choices=["m"],
                                          required=False, allow_null=True)
    notes = serializers.CharField(required=False, allow_blank=True)
    # Used ONLY to record a field-book verified renumber. Never inferred.
    verified_renumber_of_tree = serializers.IntegerField(
        required=False, allow_null=True
    )


class MeasurementImportSerializer(serializers.Serializer):
    campaign = serializers.CharField()
    rows = MeasurementImportRowSerializer(many=True)


# ------------------------------------------------- equation adoption review
class CandidateEquationInputSerializer(serializers.Serializer):
    """One candidate equation inside a review spec."""

    code = serializers.CharField(max_length=32)
    version = serializers.CharField(max_length=16)
    a = serializers.FloatField()
    b = serializers.FloatField()
    c = serializers.FloatField()
    dbh_min_cm = serializers.FloatField()
    dbh_max_cm = serializers.FloatField()
    height_required = serializers.BooleanField(required=False, default=True)
    residual_sigma = serializers.FloatField()
    citation = serializers.CharField(max_length=240)
    form = serializers.CharField(
        max_length=64, required=False,
        default="agb = a * dbh_cm^b * height_m^c")


class ReviewCreateSerializer(serializers.Serializer):
    label = serializers.CharField(max_length=120)
    # {species_code: candidate equation fields}
    candidate_spec = serializers.DictField(
        child=CandidateEquationInputSerializer())
    actor = serializers.CharField(max_length=80, required=False,
                                  allow_blank=True, default="")
    note = serializers.CharField(max_length=400, required=False,
                                 allow_blank=True, default="")


class ReviewActionSerializer(serializers.Serializer):
    actor = serializers.CharField(max_length=80, required=False,
                                  allow_blank=True, default="")
    note = serializers.CharField(max_length=400, required=False,
                                 allow_blank=True, default="")


class ReviewCompareSerializer(ReviewActionSerializer):
    baseline_version = serializers.IntegerField()


class ReviewApproveSerializer(ReviewActionSerializer):
    comparison_id = serializers.IntegerField(required=False, allow_null=True)


class ReviewEventSerializer(serializers.ModelSerializer):
    class Meta:
        model = ReviewEvent
        fields = ["id", "event", "actor", "note", "payload", "created_at"]


class ReviewComparisonSerializer(serializers.ModelSerializer):
    class Meta:
        model = ReviewComparison
        fields = [
            "id", "review", "baseline_version", "status",
            "lock_fingerprints", "coverage_matrix", "difference_payload",
            "incomplete_reasons", "baseline_reproduced", "created_at",
        ]


class ReviewSerializer(serializers.ModelSerializer):
    events = ReviewEventSerializer(many=True, read_only=True)
    approved_version_id = serializers.SerializerMethodField()

    class Meta:
        model = EquationAdoptionReview
        fields = [
            "id", "label", "status", "candidate_spec", "species_scope",
            "validation_payload", "validated_at", "approved_at",
            "withdrawn_at", "created_at", "events", "approved_version_id",
        ]

    def get_approved_version_id(self, obj):
        v = getattr(obj, "generated_version", None)
        return v.id if v else None
