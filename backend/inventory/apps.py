from django.apps import AppConfig
from django.db.models.signals import m2m_changed


class InventoryConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "inventory"
    verbose_name = "Permanent-plot remeasurement inventory"

    def ready(self):
        # A validated/approved candidate's applicability (species list) is
        # part of the frozen coefficients a comparison was run against;
        # changing it afterwards would make the audit trail lie. Block any
        # ManyToMany write once a candidate leaves the "candidate" state.
        from inventory.models import (
            CANDIDATE_APPROVED,
            CANDIDATE_VALIDATED,
            EquationCandidate,
        )

        def _freeze_candidate_species(sender, instance, action, **kwargs):
            if action not in ("pre_add", "pre_remove", "pre_clear"):
                return
            if instance.status in (CANDIDATE_VALIDATED, CANDIDATE_APPROVED):
                raise PermissionError(
                    f"Candidate {instance.code} v{instance.version} is "
                    f"{instance.status}; its applicable species are frozen. "
                    "Withdraw and create a new candidate to change coverage."
                )

        m2m_changed.connect(
            _freeze_candidate_species,
            sender=EquationCandidate.species.through,
            dispatch_uid="freeze-validated-candidate-species",
        )
