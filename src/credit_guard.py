from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class CreditBalance:
    available: int | None = None
    raw: dict | None = None
    error: str | None = None

    @property
    def is_available(self) -> bool:
        return self.available is not None

    def display(self) -> str:
        if self.available is not None:
            return f"{self.available:,}"
        if self.error:
            return f"unavailable ({self.error})"
        return "unavailable"


@dataclass(frozen=True)
class CreditRiskAssessment:
    operation_name: str
    record_count: int
    estimated_credit_risk: str
    configured_limit: int | None = None
    cached_or_skipped_count: int = 0

    @property
    def may_use_credits(self) -> bool:
        return self.record_count > 0


def format_credit_approval_message(assessment: CreditRiskAssessment, balance: CreditBalance) -> str:
    lines = [
        "WARNING: This operation may consume Apollo credits.",
        f"Operation: {assessment.operation_name}",
        f"Estimated records/API searches that may use credits: {assessment.record_count:,}",
        f"Estimated credit risk: {assessment.estimated_credit_risk}",
    ]
    if assessment.configured_limit is not None:
        lines.append(f"Configured safety limit: {assessment.configured_limit:,}")
    if assessment.cached_or_skipped_count:
        lines.append(f"Cached/skipped records not expected to use credits: {assessment.cached_or_skipped_count:,}")
    lines.append(f"Current Apollo credits: {balance.display()}")
    if balance.available is not None and balance.available < assessment.record_count:
        lines.append("Risk warning: estimated records exceed current available credits.")
    if balance.available is None:
        lines.append("Apollo credit balance is not available through the configured API settings.")
    lines.append("Continue? Type YES:")
    return "\n".join(lines)


def require_credit_approval(
    assessment: CreditRiskAssessment,
    balance: CreditBalance,
    input_func: Callable[[str], str] = input,
    output_func: Callable[[str], None] = print,
) -> bool:
    if not assessment.may_use_credits:
        output_func(f"No credit-risk work to run for {assessment.operation_name}.")
        return False
    message = format_credit_approval_message(assessment, balance)
    output_func(message)
    confirmation = input_func("")
    if confirmation == "YES":
        return True
    output_func("Approval rejected. Expected exact uppercase YES. No Apollo credit-risk operation was started.")
    return False


def format_credit_after_report(
    operation_name: str,
    before: CreditBalance,
    after: CreditBalance,
    success_count: int,
    skipped_count: int = 0,
    failed_count: int = 0,
) -> str:
    used = "unknown"
    if before.available is not None and after.available is not None:
        used = f"{max(before.available - after.available, 0):,}"
    return "\n".join(
        [
            f"{operation_name} credit report:",
            f"Credits before: {before.display()}",
            f"Credits after: {after.display()}",
            f"Estimated/observed credits used: {used}",
            f"Successful records: {success_count:,}",
            f"Skipped records: {skipped_count:,}",
            f"Failed records: {failed_count:,}",
        ]
    )
