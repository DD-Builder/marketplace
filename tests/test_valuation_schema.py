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



def test_an_appraisal_survives_a_missing_deal_score():
    """Observed in a live run: the model returned a complete valuation for a wristwatch
    and omitted deal_score, so pydantic rejected the whole thing and the lot went
    unvalued. The field is advisory — compute_deal_score is authoritative — so losing a
    paid-for appraisal over it was pure waste."""
    from dealfinder.core.schemas import AppraisalResult

    result = AppraisalResult.model_validate({
        "identified_item": "Men's stainless steel wristwatch",
        "est_asis_value_cents": 30000,
        "est_restored_resale_value_cents": 30000,
        "est_restoration_cost_cents": 0,
        "est_restoration_effort_hours": 0.0,
        "confidence": 0.6,
    })

    assert result.est_asis_value_cents == 30000
    assert result.deal_score == 0.0


def test_an_out_of_range_deal_score_is_still_rejected():
    """Defaulting the field must not turn off its validation — a 900 would sail into the
    ranking maths as if the model were certain."""
    import pytest
    from pydantic import ValidationError

    from dealfinder.core.schemas import AppraisalResult

    with pytest.raises(ValidationError):
        AppraisalResult.model_validate({
            "identified_item": "x", "est_asis_value_cents": 1,
            "est_restored_resale_value_cents": 1, "est_restoration_cost_cents": 0,
            "est_restoration_effort_hours": 0.0, "confidence": 0.5, "deal_score": 900,
        })
