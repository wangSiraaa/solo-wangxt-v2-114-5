from rest_framework import serializers

from inventory.models import (
    AllometricEquation,
    Campaign,
    CandidateEvent,
    EquationCandidate,
    EquationReview,
    EstimateVersion,
    IdentityConflict,
    Plot,
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
            "generated_by_review", "created_at", "confirmed_at",
        ]
        read_only_fields = [
            "status", "design_snapshot", "result_payload",
            "equation_checksum", "generated_by_review", "confirmed_at",
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
class CandidateEventSerializer(serializers.ModelSerializer):
    class Meta:
        model = CandidateEvent
        fields = ["id", "event", "actor", "note", "payload", "created_at"]


class ReviewEventSerializer(serializers.ModelSerializer):
    class Meta:
        model = ReviewEvent
        fields = ["id", "event", "actor", "note", "payload", "created_at"]


class EquationCandidateSerializer(serializers.ModelSerializer):
    species_codes = serializers.SlugRelatedField(
        many=True, read_only=True, slug_field="code", source="species"
    )
    n_open_reviews = serializers.SerializerMethodField()
    n_reviews = serializers.SerializerMethodField()

    class Meta:
        model = EquationCandidate
        fields = [
            "id", "code", "version", "species_codes", "status", "form",
            "a", "b", "c", "dbh_min_cm", "dbh_max_cm",
            "height_required", "residual_sigma", "citation",
            "validation_result", "validated_at", "withdrawn_at",
            "created_at", "n_open_reviews", "n_reviews",
        ]
        read_only_fields = ["status", "validation_result", "validated_at",
                            "withdrawn_at", "created_at"]

    def get_n_open_reviews(self, obj):
        return obj.reviews.filter(status="open").count()

    def get_n_reviews(self, obj):
        return obj.reviews.count()


class EquationCandidateCreateSerializer(serializers.Serializer):
    code = serializers.CharField(max_length=32)
    version = serializers.CharField(max_length=16)
    species_codes = serializers.ListField(
        child=serializers.CharField(), allow_empty=False)
    form = serializers.CharField(
        max_length=64, required=False,
        default="agb = a * dbh_cm^b * height_m^c")
    a = serializers.FloatField()
    b = serializers.FloatField()
    c = serializers.FloatField()
    dbh_min_cm = serializers.FloatField()
    dbh_max_cm = serializers.FloatField()
    height_required = serializers.BooleanField(required=False, default=True)
    residual_sigma = serializers.FloatField()
    citation = serializers.CharField(max_length=240, required=False,
                                     allow_blank=True)


class ReviewSummarySerializer(serializers.ModelSerializer):
    candidate_label = serializers.SerializerMethodField()
    baseline_label = serializers.SerializerMethodField()
    new_version_id = serializers.SerializerMethodField()
    net_delta_mg = serializers.SerializerMethodField()

    class Meta:
        model = EquationReview
        fields = [
            "id", "label", "candidate", "baseline_version", "status",
            "coverage_status", "candidate_label", "baseline_label",
            "net_delta_mg", "new_version_id",
            "compared_at", "approved_at", "withdrawn_at", "created_at",
        ]

    def get_candidate_label(self, obj):
        c = obj.candidate
        return f"{c.code}@{c.version}"

    def get_baseline_label(self, obj):
        b = obj.baseline_version
        return f"#{b.id} {b.label} [{b.status}]"

    def get_new_version_id(self, obj):
        v = obj.generated_versions.order_by("id").first()
        return v.id if v else None

    def get_net_delta_mg(self, obj):
        if obj.comparison:
            return obj.comparison["population"]["net_change"]["delta_mg"]
        return None


class ReviewDetailSerializer(ReviewSummarySerializer):
    class Meta(ReviewSummarySerializer.Meta):
        fields = ReviewSummarySerializer.Meta.fields + [
            "lock_snapshot", "lock_checksum", "comparison",
            "candidate_checksum_at_comparison",
        ]


class ReviewCreateSerializer(serializers.Serializer):
    candidate_id = serializers.IntegerField()
    baseline_version_id = serializers.IntegerField()
    label = serializers.CharField(max_length=160, required=False,
                                  allow_blank=True)


class ReviewApproveSerializer(serializers.Serializer):
    label = serializers.CharField(max_length=160, required=False,
                                  allow_blank=True)
    actor = serializers.CharField(max_length=80, required=False)


class CandidateActionSerializer(serializers.Serializer):
    note = serializers.CharField(required=False, allow_blank=True)
    actor = serializers.CharField(max_length=80, required=False)
