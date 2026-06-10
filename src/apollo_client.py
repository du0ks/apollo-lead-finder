from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from typing import Any

import requests
from dotenv import load_dotenv
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from .credit_guard import CreditBalance
from .database import Database
from .utils import campaign_raw_dir, dump_json, utc_now_iso


class ApolloAPIError(RuntimeError):
    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        endpoint: str = "",
        response_body: str = "",
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.endpoint = endpoint
        self.response_body = response_body


class ApolloRetryableError(ApolloAPIError):
    pass


class ApolloClient:
    ORGANIZATION_SEARCH_PATH = "/mixed_companies/search"
    PEOPLE_SEARCH_PATH = "/mixed_people/api_search"
    PEOPLE_BULK_ENRICH_PATH = "/people/bulk_match"
    PEOPLE_ENRICH_PATH = "/people/match"
    WEBHOOK_RESULT_PATH = "/webhook_result"
    MAX_ORGANIZATION_SEARCH_PAGE_SIZE = 100
    RETRYABLE_STATUS_CODES = {408, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524}

    def __init__(
        self,
        db: Database,
        api_key: str | None = None,
        base_url: str | None = None,
        min_interval_seconds: float = 0.5,
    ) -> None:
        load_dotenv()
        self.api_key = (api_key or os.getenv("APOLLO_API_KEY", "")).strip()
        self.base_url = (base_url or os.getenv("APOLLO_BASE_URL", "https://api.apollo.io/api/v1")).rstrip("/")
        self.credits_path = os.getenv("APOLLO_CREDITS_PATH", "").strip()
        self.webhook_url = os.getenv("APOLLO_WEBHOOK_URL", "").strip()
        self.min_interval_seconds = min_interval_seconds
        self.last_call_at = 0.0
        self.db = db
        self.logger = logging.getLogger(__name__)
        self.api_logger = logging.getLogger("api_calls")

    def validate_api_key(self) -> None:
        if not self.api_key:
            raise ValueError("APOLLO_API_KEY is missing. Copy .env.example to .env and add your Apollo API key.")
        lowered = self.api_key.lower()
        if lowered in {"your_apollo_api_key_here", "your_api_key_here", "apollo_api_key", "..."}:
            raise ValueError("APOLLO_API_KEY is still the example placeholder. Replace it with your real Apollo API key.")

    def organization_search(
        self,
        country: str,
        keyword: str,
        per_page: int,
        campaign_name: str,
        page: int = 1,
        use_cache: bool = True,
    ) -> dict[str, Any]:
        payload = self.organization_search_payload(country, keyword, per_page, page)
        return self._cached_post(self.ORGANIZATION_SEARCH_PATH, payload, campaign_name, use_cache)

    def organization_search_payload(self, country: str, keyword: str, per_page: int, page: int = 1) -> dict[str, Any]:
        return {
            "q_organization_keyword_tags": [keyword],
            "organization_locations": [country],
            "page": page,
            "per_page": min(per_page, self.MAX_ORGANIZATION_SEARCH_PAGE_SIZE),
        }

    def organization_search_cache_key(self, country: str, keyword: str, per_page: int, page: int = 1) -> str:
        return self._cache_key(self.ORGANIZATION_SEARCH_PATH, self.organization_search_payload(country, keyword, per_page, page))

    def is_organization_search_cached(self, country: str, keyword: str, per_page: int, page: int = 1) -> bool:
        return self.db.get_cached_search(self.organization_search_cache_key(country, keyword, per_page, page)) is not None

    def people_search(
        self,
        organization_id: str | None,
        domain: str | None,
        titles: list[str],
        campaign_name: str,
        per_page: int = 50,
        use_cache: bool = True,
        email_statuses: list[str] | None = None,
        include_similar_titles: bool = True,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "person_titles": titles,
            "page": 1,
            "per_page": per_page,
            "include_similar_titles": include_similar_titles,
        }
        if email_statuses:
            payload["contact_email_status"] = email_statuses
        if organization_id:
            payload["organization_ids"] = [organization_id]
        if domain:
            payload["q_organization_domains_list"] = [domain]
        return self._cached_post(self.PEOPLE_SEARCH_PATH, payload, campaign_name, use_cache)

    def enrich_people_bulk(
        self,
        people: list[dict[str, Any]],
        campaign_name: str,
        reveal_personal_emails: bool = True,
        run_waterfall_email: bool = True,
    ) -> dict[str, Any]:
        if len(people) > 10:
            raise ValueError("Bulk People Enrichment supports up to 10 people per API call")
        payload = {"details": people}
        params = {
            "reveal_personal_emails": str(reveal_personal_emails).lower(),
        }
        if run_waterfall_email and self.webhook_url:
            params["run_waterfall_email"] = "true"
            params["webhook_url"] = self.webhook_url
        return self._post(self.PEOPLE_BULK_ENRICH_PATH, payload, campaign_name, params=params)

    def enrich_person(
        self,
        person: dict[str, Any],
        campaign_name: str,
        reveal_personal_emails: bool = True,
        run_waterfall_email: bool = True,
    ) -> dict[str, Any]:
        params = {
            "reveal_personal_emails": str(reveal_personal_emails).lower(),
        }
        if run_waterfall_email and self.webhook_url:
            params["run_waterfall_email"] = "true"
            params["webhook_url"] = self.webhook_url
        return self._post(self.PEOPLE_ENRICH_PATH, person, campaign_name, params=params)

    def poll_webhook_result(self, request_id: str | int, campaign_name: str) -> dict[str, Any]:
        return self._get(f"{self.WEBHOOK_RESULT_PATH}/{request_id}", campaign_name=campaign_name)

    def get_credit_balance(self, campaign_name: str | None = None) -> CreditBalance:
        if not self.credits_path:
            return CreditBalance(error="APOLLO_CREDITS_PATH is not configured")
        try:
            response = self._get(self.credits_path, campaign_name=campaign_name)
        except Exception as exc:
            return CreditBalance(error=str(exc))
        available = self._extract_credit_balance(response)
        if available is None:
            return CreditBalance(raw=response, error="credit balance field was not found in response")
        return CreditBalance(available=available, raw=response)

    def _cached_post(
        self,
        endpoint: str,
        payload: dict[str, Any],
        campaign_name: str,
        use_cache: bool,
    ) -> dict[str, Any]:
        cache_key = self._cache_key(endpoint, payload)
        if use_cache:
            cached = self.db.get_cached_search(cache_key)
            if cached is not None:
                self.db.log_api_call("POST_CACHE", endpoint, 200, cache_key=cache_key, campaign_name=campaign_name)
                self.api_logger.info("cache_hit endpoint=%s cache_key=%s", endpoint, cache_key)
                return cached
        response = self._post(endpoint, payload, campaign_name, cache_key=cache_key)
        self.db.set_cached_search(cache_key, endpoint, response)
        return response

    @retry(
        retry=retry_if_exception_type(ApolloRetryableError),
        wait=wait_exponential(multiplier=1, min=1, max=30),
        stop=stop_after_attempt(5),
        reraise=True,
    )
    def _post(
        self,
        endpoint: str,
        payload: dict[str, Any],
        campaign_name: str,
        cache_key: str | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.validate_api_key()
        self._rate_limit()
        url = f"{self.base_url}{endpoint}"
        request_hash = self._cache_key(endpoint, {"payload": payload, "params": params or {}})
        headers = {
            "Content-Type": "application/json",
            "x-api-key": self.api_key,
        }
        try:
            response = requests.post(url, headers=headers, json=payload, params=params, timeout=60)
        except requests.RequestException as exc:
            raise ApolloRetryableError(
                f"Apollo network error while calling {endpoint}: {exc}",
                endpoint=endpoint,
                response_body=str(exc),
            ) from exc
        self.db.log_api_call("POST", endpoint, response.status_code, request_hash, cache_key, campaign_name)
        self.api_logger.info("post endpoint=%s status=%s request_hash=%s", endpoint, response.status_code, request_hash)

        if self._is_retryable_status(response.status_code):
            raise ApolloRetryableError(
                self._format_api_error("Apollo transient error", endpoint, response.status_code, response.text),
                status_code=response.status_code,
                endpoint=endpoint,
                response_body=response.text,
            )
        if response.status_code >= 400:
            raise ApolloAPIError(
                self._format_api_error("Apollo API error", endpoint, response.status_code, response.text),
                status_code=response.status_code,
                endpoint=endpoint,
                response_body=response.text,
            )

        data = response.json()
        self._save_raw(endpoint, request_hash, data, campaign_name)
        return data

    @retry(
        retry=retry_if_exception_type(ApolloRetryableError),
        wait=wait_exponential(multiplier=1, min=1, max=30),
        stop=stop_after_attempt(5),
        reraise=True,
    )
    def _get(self, endpoint: str, campaign_name: str | None = None) -> dict[str, Any]:
        self.validate_api_key()
        self._rate_limit()
        path = endpoint if endpoint.startswith("/") else f"/{endpoint}"
        url = f"{self.base_url}{path}"
        headers = {
            "Content-Type": "application/json",
            "x-api-key": self.api_key,
        }
        try:
            response = requests.get(url, headers=headers, timeout=60)
        except requests.RequestException as exc:
            raise ApolloRetryableError(
                f"Apollo network error while calling {path}: {exc}",
                endpoint=path,
                response_body=str(exc),
            ) from exc
        self.db.log_api_call("GET", path, response.status_code, campaign_name=campaign_name)
        self.api_logger.info("get endpoint=%s status=%s", path, response.status_code)

        if self._is_retryable_status(response.status_code):
            raise ApolloRetryableError(
                self._format_api_error("Apollo transient error", path, response.status_code, response.text),
                status_code=response.status_code,
                endpoint=path,
                response_body=response.text,
            )
        if response.status_code >= 400:
            raise ApolloAPIError(
                self._format_api_error("Apollo API error", path, response.status_code, response.text),
                status_code=response.status_code,
                endpoint=path,
                response_body=response.text,
            )

        data = response.json()
        self._save_raw(path, self._cache_key(path, {}), data, campaign_name)
        return data

    def _rate_limit(self) -> None:
        elapsed = time.monotonic() - self.last_call_at
        if elapsed < self.min_interval_seconds:
            time.sleep(self.min_interval_seconds - elapsed)
        self.last_call_at = time.monotonic()

    def _save_raw(self, endpoint: str, request_hash: str, payload: dict[str, Any], campaign_name: str | None) -> None:
        safe_endpoint = endpoint.strip("/").replace("/", "_")
        timestamp = utc_now_iso().replace(":", "-")
        dump_json(campaign_raw_dir(campaign_name) / f"{timestamp}_{safe_endpoint}_{request_hash[:10]}.json", payload)

    @staticmethod
    def _cache_key(endpoint: str, payload: dict[str, Any]) -> str:
        body = json.dumps({"endpoint": endpoint, "payload": payload}, sort_keys=True)
        return hashlib.sha256(body.encode("utf-8")).hexdigest()

    @classmethod
    def _is_retryable_status(cls, status_code: int) -> bool:
        return status_code in cls.RETRYABLE_STATUS_CODES

    @staticmethod
    def _format_api_error(prefix: str, endpoint: str, status_code: int, body: str) -> str:
        clean_body = ApolloClient._clean_error_body(body)
        return f"{prefix} {status_code} while calling {endpoint}: {clean_body}"

    @staticmethod
    def _clean_error_body(body: str) -> str:
        stripped = body.strip()
        title_match = re.search(r"<title>(.*?)</title>", stripped, flags=re.IGNORECASE | re.DOTALL)
        if title_match:
            return re.sub(r"\s+", " ", title_match.group(1)).strip()
        return stripped[:500]

    @staticmethod
    def _extract_credit_balance(payload: dict[str, Any]) -> int | None:
        candidate_keys = (
            "credits_remaining",
            "remaining_credits",
            "available_credits",
            "credits",
            "email_credits_remaining",
        )
        stack: list[Any] = [payload]
        while stack:
            current = stack.pop()
            if isinstance(current, dict):
                for key, value in current.items():
                    if key in candidate_keys and isinstance(value, int):
                        return value
                    if key in candidate_keys and isinstance(value, str) and value.isdigit():
                        return int(value)
                    if isinstance(value, dict | list):
                        stack.append(value)
            elif isinstance(current, list):
                stack.extend(current)
        return None
