"""
REST API for the equation adoption review main scenario.

POST   /equation-reviews/                      create candidate
POST   /equation-reviews/{id}/validate/        candidate -> validated
POST   /equation-reviews/{id}/compare/         lock a confirmed baseline +
                                               impact comparison
POST   /equation-reviews/{id}/withdraw/        validated/candidate -> withdrawn
POST   /equation-reviews/{id}/approve/         validated -> approved (mints ONE
                                               new confirmed EstimateVersion)
GET    /equation-reviews/                      history (events embedded)
GET    /equation-reviews/{id}/                 full review + audit trail
GET    /equation-reviews/{id}/comparisons/     coverage matrices & diffs
"""
from django.shortcuts import get_object_or_404
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from inventory.models import (
    EquationAdoptionReview,
    EstimateVersion,
    ReviewComparison,
    ReviewEvent,
    VERSION_CONFIRMED,
)
from inventory.serializers import (
    ReviewActionSerializer as ReviewActionSerializerSafe,
    ReviewApproveSerializer,
    ReviewCompareSerializer,
    ReviewComparisonSerializer,
    ReviewCreateSerializer,
    ReviewSerializer,
)
from inventory.services import review as review_service
from inventory.services.review import (
    ConcurrentApprovalError,
    ReviewLockError,
    ReviewStateError,
    ReviewValidationError,
)


class EquationReviewViewSet(viewsets.ViewSet):
    def list(self, request):
        qs = (EquationAdoptionReview.objects
              .prefetch_related("events", "generated_version")
              .all())
        return Response(ReviewSerializer(qs, many=True).data)

    def retrieve(self, request, pk=None):
        review = self._get(pk)
        return Response(ReviewSerializer(review).data)

    def create(self, request):
        """
        Body: {"label": ..., "candidate_spec": {SPECIES: {code, version,
               a,b,c, dbh_min_cm, dbh_max_cm, residual_sigma, citation,...}}}
        """
        ser = ReviewCreateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data
        spec = {sp: dict(fields) for sp, fields in
                data["candidate_spec"].items()}
        review = EquationAdoptionReview.objects.create(
            label=data["label"], candidate_spec=spec,
            species_scope=sorted(spec))
        ReviewEvent.objects.create(
            review=review, event=ReviewEvent.EVENT_CREATED,
            actor=data.get("actor", ""), note=data.get("note", ""),
            payload={"species_scope": sorted(spec),
                    "candidate_spec": spec})
        return Response(ReviewSerializer(review).data,
                        status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"])
    def validate(self, request, pk=None):
        review = self._get(pk)
        ser = _action_ser(ReviewActionSerializerSafe, request)
        try:
            review_service.validate_candidate(
                review, actor=ser.get("actor", ""), note=ser.get("note", ""))
        except ReviewStateError as e:
            return _conflict(e)
        except ReviewValidationError as e:
            return Response(
                {"detail": str(e), "validation": e.payload},
                status=status.HTTP_422_UNPROCESSABLE_ENTITY)
        review.refresh_from_db()
        return Response(ReviewSerializer(review).data)

    @action(detail=True, methods=["post"], url_path="compare")
    def compare(self, request, pk=None):
        review = self._get(pk)
        ser = ReviewCompareSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data
        baseline = get_object_or_404(EstimateVersion,
                                     pk=data["baseline_version"])
        if baseline.status != VERSION_CONFIRMED:
            return Response(
                {"detail": "baseline EstimateVersion must be confirmed "
                           "before it can be locked for comparison."},
                status=status.HTTP_400_BAD_REQUEST)
        try:
            comparison = review_service.create_comparison(
                review, baseline, actor=data.get("actor", ""),
                note=data.get("note", ""))
        except ReviewStateError as e:
            return _conflict(e)
        except ReviewLockError as e:
            review.refresh_from_db()
            return Response(
                {"detail": "comparison refused: the locked baseline is not "
                           "reproducible from current data/identity/frame; "
                           "differences cannot be attributed to the equation "
                           "alone.",
                 "lock": e.detail},
                status=status.HTTP_409_CONFLICT)
        code = (status.HTTP_201_CREATED if comparison.status == "complete"
                else status.HTTP_202_ACCEPTED)
        return Response(ReviewComparisonSerializer(comparison).data,
                        status=code)

    @action(detail=True, methods=["post"])
    def withdraw(self, request, pk=None):
        review = self._get(pk)
        ser = _action_ser(ReviewActionSerializerSafe, request)
        try:
            review_service.withdraw_review(
                review, actor=ser.get("actor", ""), note=ser.get("note", ""))
        except ReviewStateError as e:
            return _conflict(e)
        review.refresh_from_db()
        return Response(ReviewSerializer(review).data)

    @action(detail=True, methods=["post"])
    def approve(self, request, pk=None):
        review = self._get(pk)
        ser = ReviewApproveSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data
        comparison = None
        if data.get("comparison_id"):
            comparison = get_object_or_404(
                ReviewComparison, pk=data["comparison_id"], review=review)
        try:
            review, new_version = review_service.approve_review(
                review, actor=data.get("actor", ""), comparison=comparison,
                note=data.get("note", ""))
        except ReviewStateError as e:
            return _conflict(e)
        except ReviewLockError as e:
            return Response(
                {"detail": "approval blocked: locked inputs drifted since "
                           "comparison.", "lock": e.detail},
                status=status.HTTP_409_CONFLICT)
        except ConcurrentApprovalError as e:
            review.refresh_from_db()
            return Response(
                {"detail": "a concurrent approval already generated the "
                           f"single new version #{e.existing_version.id}; "
                           "no duplicate version was created.",
                 "review": ReviewSerializer(review).data,
                 "existing_version_id": e.existing_version.id},
                status=status.HTTP_409_CONFLICT)
        review.refresh_from_db()
        return Response(
            {"review": ReviewSerializer(review).data,
             "new_estimate_version_id": new_version.id},
            status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["get"])
    def comparisons(self, request, pk=None):
        review = self._get(pk)
        qs = review.comparisons.all()
        state = request.query_params.get("status")
        if state:
            qs = qs.filter(status=state)
        return Response(ReviewComparisonSerializer(qs, many=True).data)

    def _get(self, pk):
        return get_object_or_404(
            EquationAdoptionReview.objects
            .prefetch_related("events", "generated_version"),
            pk=pk)


def _action_ser(klass, request):
    s = klass(data=request.data)
    s.is_valid(raise_exception=True)
    return s.validated_data


def _conflict(exc):
    return Response({"detail": str(exc)},
                    status=status.HTTP_409_CONFLICT)
