from src.credit_guard import CreditBalance, CreditRiskAssessment, format_credit_approval_message, require_credit_approval


def test_credit_approval_message_includes_balance_and_limit() -> None:
    assessment = CreditRiskAssessment(
        operation_name="Bulk People Enrichment",
        record_count=12,
        estimated_credit_risk="up to 12 people enrichment credits",
        configured_limit=500,
        cached_or_skipped_count=3,
    )

    message = format_credit_approval_message(assessment, CreditBalance(available=4000))

    assert "Bulk People Enrichment" in message
    assert "12" in message
    assert "500" in message
    assert "4,000" in message
    assert "Cached/skipped" in message


def test_requires_exact_yes() -> None:
    assessment = CreditRiskAssessment("Organization Search", 1, "up to 1 credit-risk request")
    outputs: list[str] = []

    approved = require_credit_approval(
        assessment,
        CreditBalance(error="not configured"),
        input_func=lambda _: "yes",
        output_func=outputs.append,
    )

    assert approved is False
    assert outputs
