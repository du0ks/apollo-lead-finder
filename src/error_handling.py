from __future__ import annotations

import traceback
from dataclasses import dataclass

from .apollo_client import ApolloAPIError, ApolloRetryableError


@dataclass
class ErrorExplanation:
    title: str
    cause: str
    action: str
    technical_details: str = ""

    def format(self) -> str:
        lines = [
            f"ERROR: {self.title}",
            f"Cause: {self.cause}",
            f"What to do: {self.action}",
        ]
        if self.technical_details:
            lines.append(f"Technical details: {self.technical_details}")
        return "\n".join(lines)


def explain_exception(exc: Exception, operation: str = "Operation") -> ErrorExplanation:
    if isinstance(exc, ApolloAPIError):
        return explain_apollo_error(exc, operation)
    if isinstance(exc, ValueError):
        return ErrorExplanation(
            title=f"{operation} configuration error",
            cause=str(exc),
            action="Fix the configuration or .env value, then run the command again.",
        )
    return ErrorExplanation(
        title=f"{operation} failed unexpectedly",
        cause=str(exc) or exc.__class__.__name__,
        action="Check logs/app.log for the full traceback. If it repeats, keep the command output and logs for debugging.",
        technical_details=exc.__class__.__name__,
    )


def explain_apollo_error(exc: ApolloAPIError, operation: str = "Apollo request") -> ErrorExplanation:
    status = exc.status_code
    endpoint = exc.endpoint or _endpoint_from_message(str(exc))
    technical = str(exc)
    lowered = technical.lower()

    if "insufficient credits" in lowered or "increase your number of lead credits" in lowered:
        return ErrorExplanation(
            title=f"{operation} stopped: Apollo lead/email credits are insufficient",
            cause=(
                "Apollo accepted your API key, but the enrichment endpoint says the account does not have enough "
                "lead/email credits for more enrichment. This is separate from search credits and can happen mid-run."
            ),
            action=(
                "Add/restore Apollo lead/email credits, lower limits.max_total_people_to_enrich, or rerun later with "
                "--resume --enrich-selected. Completed enrichments are already saved in SQLite and partial Excel is exported."
            ),
            technical_details=_details(status, endpoint, technical),
        )

    if status == 401:
        return ErrorExplanation(
            title=f"{operation} failed: Apollo rejected the API key",
            cause=(
                "Apollo returned 401 Invalid access credentials. This is an API key/authentication problem, "
                "not a credit-balance problem."
            ),
            action=(
                "Open .env and check APOLLO_API_KEY. Make sure it is the newly generated key with no quotes, "
                "no extra spaces, and permission for this endpoint. For Organization Search, enable "
                "api/v1/mixed_companies/search or api/v1/organizations/search in Apollo key permissions, "
                "then rerun the command."
            ),
            technical_details=_details(status, endpoint, technical),
        )
    if status == 403:
        return ErrorExplanation(
            title=f"{operation} failed: Apollo key lacks permission",
            cause="Apollo accepted the key but says this endpoint is not accessible for that key.",
            action=(
                "Edit the Apollo API key permissions and enable the endpoint shown below, or generate a new dedicated key "
                "with that endpoint enabled. If the endpoint is /webhook_result, keep polling disabled and use webhook.site collection."
            ),
            technical_details=_details(status, endpoint, technical),
        )
    if status == 422:
        return ErrorExplanation(
            title=f"{operation} failed: Apollo rejected the request parameters",
            cause="The API key worked, but one or more request fields are invalid for Apollo.",
            action="Check the endpoint and request limits. Common fixes: lower per_page, remove unsupported filters, or validate country/title values.",
            technical_details=_details(status, endpoint, technical),
        )
    if status == 429:
        return ErrorExplanation(
            title=f"{operation} failed: Apollo rate limit reached",
            cause="Apollo returned 429, meaning too many requests in a short time.",
            action="Wait a few minutes and rerun with --resume so cached searches are reused.",
            technical_details=_details(status, endpoint, technical),
        )
    if isinstance(exc, ApolloRetryableError) or (status and status >= 500):
        return ErrorExplanation(
            title=f"{operation} failed: temporary Apollo/server problem",
            cause="Apollo or an upstream service returned a temporary error after retries.",
            action="Wait and rerun with --resume. The CLI will reuse cached successful searches and continue from there.",
            technical_details=_details(status, endpoint, technical),
        )
    if status and 400 <= status < 500:
        return ErrorExplanation(
            title=f"{operation} failed: Apollo client-side error",
            cause="Apollo rejected the request.",
            action="Read the technical details below, check API key permissions and config filters, then rerun.",
            technical_details=_details(status, endpoint, technical),
        )
    return ErrorExplanation(
        title=f"{operation} failed: Apollo request error",
        cause="The Apollo request failed before a normal response was returned.",
        action="Check internet connection, API key, and logs/app.log, then rerun with --resume if appropriate.",
        technical_details=technical,
    )


def log_unexpected_exception(logger, exc: Exception) -> None:
    logger.error("Unexpected error: %s\n%s", exc, traceback.format_exc())


def _details(status: int | None, endpoint: str, message: str) -> str:
    pieces = []
    if status:
        pieces.append(f"status={status}")
    if endpoint:
        pieces.append(f"endpoint={endpoint}")
    pieces.append(message)
    return " | ".join(pieces)


def _endpoint_from_message(message: str) -> str:
    marker = " while calling "
    if marker not in message:
        return ""
    tail = message.split(marker, 1)[1]
    return tail.split(":", 1)[0].strip()
