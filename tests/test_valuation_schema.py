"""Structured-output schema validation for the valuation DTOs."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from dealfinder.core.schemas import AppraisalResult


def test_appraisal_result_validates_good_payload():
    result = AppraisalResult.model_validate(
        {
            "identified_item": "Danish teak sideboard",
            "style_era": "Mid-century, 1960s",
            "maker_guess": None,
            "materials": ["teak", "brass"],
            "condition_assessment": "Scratched top, sticky drawer.",
            "est_asis_value_cents": 15000,
            "est_restored_resale_value_cents": 60000,
            "est_restoration_cost_cents": 5000,
            "est_restoration_effort_hours": 4.0,
            "confidence": 0.7,
            "deal_score": 68.0,
            "reasoning": "Underpriced for the maker and materials.",
        }
    )
    assert result.confidence == 0.7
    assert result.materials == ["teak", "brass"]


def test_confidence_out_of_range_rejected():
    with pytest.raises(ValidationError):
        AppraisalResult.model_validate(
            {
                "identified_item": "x",
                "est_asis_value_cents": 1,
                "est_restored_resale_value_cents": 1,
                "est_restoration_cost_cents": 1,
                "est_restoration_effort_hours": 1.0,
                "confidence": 1.5,  # invalid
                "deal_score": 10.0,
            }
        )

