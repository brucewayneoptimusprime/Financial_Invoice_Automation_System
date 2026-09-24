from app.engine.evaluators.registry import REGISTRY, BaseParams, EvaluatorSpec, register

# Importing the modules registers their evaluators.
from app.engine.evaluators import amount, arithmetic, completeness, duplicates, vendor_po  # noqa: E402,F401

__all__ = ["REGISTRY", "BaseParams", "EvaluatorSpec", "register"]
