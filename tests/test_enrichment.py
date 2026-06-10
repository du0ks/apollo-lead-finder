from pathlib import Path

import pandas as pd

from src.apollo_client import ApolloAPIError, ApolloClient
from src.database import Database
from src.enrichment import (
    EnrichmentStoppedError,
    enrich_selected_people,
    _merge_enriched,
    _poll_waterfall_email_results,
    people_needing_enrichment,
    selected_people_for_enrichment,
)
from src.exporter import export_enriched
from src.models import EnrichedLead, Person


def test_final_export_excludes_internal_columns(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("src.utils.OUTPUT_DIR", tmp_path)
    lead = EnrichedLead(
        campaign_name="camp",
        country="Hungary",
        keyword="drilling",
        person_name="Jane Doe",
        selected_for_enrichment=True,
        already_enriched_before=True,
        email="jane@example.com",
        enriched_at="2026-01-01T00:00:00Z",
        apollo_credit_risk_note="internal",
    )

    path = export_enriched([lead])
    columns = set(pd.read_excel(path).columns)

    assert path.parent.name == "camp"
    assert "apollo_credit_risk_note" not in columns
    assert "enriched_at" not in columns
    assert "already_enriched_before" not in columns
    assert "selected_for_enrichment" not in columns
    assert {"person_name", "email", "email_status", "enrichment_status", "email_source"} <= columns


def test_merge_enriched_overwrites_obfuscated_name_and_linkedin() -> None:
    person = Person(
        campaign_name="camp",
        country="Hungary",
        keyword="casing",
        person_name="Andras Ma***s",
        first_name="Andras",
        last_name="Ma***s",
    )

    lead = _merge_enriched(
        person,
        {
            "id": "person-1",
            "name": "Andras Martins",
            "first_name": "Andras",
            "last_name": "Martins",
            "linkedin_url": "https://linkedin.com/in/andras-martins",
            "email": "andras@example.com",
            "email_status": "verified",
        },
        "enriched",
    )

    assert lead.person_name == "Andras Martins"
    assert lead.last_name == "Martins"
    assert lead.linkedin_url == "https://linkedin.com/in/andras-martins"
    assert lead.email == "andras@example.com"


def test_bulk_enrichment_omits_waterfall_without_webhook() -> None:
    captured = {}
    client = object.__new__(ApolloClient)
    client.webhook_url = ""

    def fake_post(endpoint, payload, campaign_name, cache_key=None, params=None):
        captured["endpoint"] = endpoint
        captured["payload"] = payload
        captured["params"] = params
        return {"matches": []}

    client._post = fake_post
    response = client.enrich_people_bulk([{"id": "person-1"}], "camp")

    assert response == {"matches": []}
    assert captured["endpoint"] == ApolloClient.PEOPLE_BULK_ENRICH_PATH
    assert captured["params"] == {
        "reveal_personal_emails": "true",
    }


def test_bulk_enrichment_uses_waterfall_when_webhook_is_configured() -> None:
    captured = {}
    client = object.__new__(ApolloClient)
    client.webhook_url = "https://example.com/apollo-webhook"

    def fake_post(endpoint, payload, campaign_name, cache_key=None, params=None):
        captured["params"] = params
        return {"matches": []}

    client._post = fake_post
    client.enrich_people_bulk([{"id": "person-1"}], "camp")

    assert captured["params"] == {
        "reveal_personal_emails": "true",
        "run_waterfall_email": "true",
        "webhook_url": "https://example.com/apollo-webhook",
    }


def test_cached_missing_email_is_eligible_for_retry(tmp_path: Path) -> None:
    db = Database(tmp_path / "test.sqlite")
    person = Person(
        campaign_name="camp",
        country="Hungary",
        keyword="drilling",
        person_name="Jane Doe",
        selected_for_enrichment=True,
        duplicate_key="apollo_id:person-1",
        already_enriched_before=False,
    )
    db.save_enrichment("apollo_id:person-1", {"id": "person-1", "email_status": "unavailable"})
    person.already_enriched_before = db.enrichment_has_usable_email(person.duplicate_key)

    selected = selected_people_for_enrichment([person], max_total=10)

    assert selected == [person]
    db.close()


def test_cached_unavailable_is_not_retried_by_default(tmp_path: Path) -> None:
    db = Database(tmp_path / "test.sqlite")
    person = Person(
        campaign_name="camp",
        country="Hungary",
        keyword="drilling",
        person_name="Jane Doe",
        selected_for_enrichment=True,
        duplicate_key="apollo_id:person-1",
    )
    db.save_enrichment("apollo_id:person-1", {"id": "person-1", "email_status": "unavailable"})

    assert people_needing_enrichment([person], db, max_total=10) == []
    assert people_needing_enrichment([person], db, max_total=10, retry_unavailable=True) == [person]
    db.close()


def test_cached_nested_email_is_not_eligible_for_retry(tmp_path: Path) -> None:
    db = Database(tmp_path / "test.sqlite")
    person = Person(
        campaign_name="camp",
        country="Hungary",
        keyword="drilling",
        person_name="Jane Doe",
        selected_for_enrichment=True,
        duplicate_key="apollo_id:person-1",
        already_enriched_before=False,
    )
    db.save_enrichment(
        "apollo_id:person-1",
        {"person": {"id": "person-1", "email": "jane@example.com", "email_status": "verified"}},
    )
    person.already_enriched_before = db.enrichment_has_usable_email(person.duplicate_key)

    selected = selected_people_for_enrichment([person], max_total=10)

    assert selected == []
    db.close()


def test_waterfall_poll_response_with_email_updates_lead(monkeypatch) -> None:
    monkeypatch.setenv("APOLLO_WATERFALL_POLL_ATTEMPTS", "1")
    client = object.__new__(ApolloClient)
    client.poll_webhook_result = lambda request_id, campaign_name: {
        "matches": [{"id": "person-1", "waterfall_email": "jane@example.com"}]
    }
    person = Person(
        campaign_name="camp",
        country="Hungary",
        keyword="drilling",
        person_name="Jane Doe",
        apollo_person_id="person-1",
    )

    waterfall = _poll_waterfall_email_results({"request_id": "req-1"}, client, "camp")
    lead = _merge_enriched(person, waterfall[0], "enriched")

    assert lead.email == "jane@example.com"
    assert lead.email_source == "waterfall"


def test_waterfall_poll_permission_error_is_non_fatal(monkeypatch) -> None:
    monkeypatch.setenv("APOLLO_WATERFALL_POLL_ATTEMPTS", "1")
    client = object.__new__(ApolloClient)

    def forbidden_poll(request_id, campaign_name):
        raise ApolloAPIError("Apollo API error 403 while calling /webhook_result/1: API_INACCESSIBLE")

    client.poll_webhook_result = forbidden_poll

    assert _poll_waterfall_email_results({"request_id": "1"}, client, "camp") == []


def test_waterfall_polling_is_disabled_by_default(monkeypatch) -> None:
    monkeypatch.delenv("APOLLO_WATERFALL_POLL_ATTEMPTS", raising=False)
    client = object.__new__(ApolloClient)
    called = False

    def forbidden_poll(request_id, campaign_name):
        nonlocal called
        called = True
        return {}

    client.poll_webhook_result = forbidden_poll

    assert _poll_waterfall_email_results({"request_id": "1"}, client, "camp") == []
    assert called is False


def test_enrichment_uses_single_person_fallback_when_bulk_has_no_email(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("APOLLO_SINGLE_ENRICH_FALLBACK", "true")
    db = Database(tmp_path / "test.sqlite")
    client = object.__new__(ApolloClient)
    client.enrich_people_bulk = lambda people, campaign_name: {
        "matches": [{"id": "person-1", "email_status": "unavailable"}]
    }
    client.enrich_person = lambda person, campaign_name: {
        "person": {"id": "person-1", "email": "jane@example.com", "email_status": "verified"}
    }
    person = Person(
        campaign_name="camp",
        country="Hungary",
        keyword="drilling",
        person_name="Jane Doe",
        apollo_person_id="person-1",
        selected_for_enrichment=True,
        duplicate_key="apollo_id:person-1",
    )

    leads = enrich_selected_people([person], client, db, "camp", max_total=10)

    assert leads[0].email == "jane@example.com"
    assert db.enrichment_has_usable_email("apollo_id:person-1")
    db.close()


def test_single_person_fallback_waterfall_callback_updates_lead(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("APOLLO_SINGLE_ENRICH_FALLBACK", "true")
    monkeypatch.setenv("APOLLO_WEBHOOK_WAIT_SECONDS", "1")
    monkeypatch.setattr("src.enrichment.is_webhook_site_url", lambda url: True)
    monkeypatch.setattr(
        "src.enrichment.collect_webhook_site_payloads",
        lambda webhook_url, request_ids, wait_seconds, poll_interval_seconds, progress_func=None: [
            {
                "request_id": "single-req-1",
                "people": [{"id": "person-1", "emails": [{"email": "jane@example.com"}]}],
            }
        ],
    )
    db = Database(tmp_path / "test.sqlite")
    client = object.__new__(ApolloClient)
    client.webhook_url = "https://webhook.site/7491b875-9742-4822-94c1-f7b37e81ab2a"
    client.enrich_people_bulk = lambda people, campaign_name: {
        "request_id": "bulk-req-1",
        "matches": [{"id": "person-1", "email_status": "unavailable"}],
    }
    client.enrich_person = lambda person, campaign_name: {
        "request_id": "single-req-1",
        "person": {"id": "person-1", "email_status": "unavailable"},
    }
    person = Person(
        campaign_name="camp",
        country="Hungary",
        keyword="drilling",
        person_name="Jane Doe",
        apollo_person_id="person-1",
        selected_for_enrichment=True,
        duplicate_key="apollo_id:person-1",
    )

    leads = enrich_selected_people([person], client, db, "camp", max_total=10)

    assert leads[0].email == "jane@example.com"
    assert leads[0].email_source == "waterfall"
    db.close()


def test_enrichment_stops_cleanly_with_partial_leads_on_credit_error(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("APOLLO_SINGLE_ENRICH_FALLBACK", "false")
    db = Database(tmp_path / "test.sqlite")
    client = object.__new__(ApolloClient)
    client.webhook_url = ""
    calls = {"count": 0}

    def enrich_people_bulk(people, campaign_name):
        calls["count"] += 1
        if calls["count"] == 1:
            return {"matches": [{"id": "person-1", "email": "jane@example.com", "email_status": "verified"}]}
        raise ApolloAPIError(
            "Apollo API error 422 while calling /people/bulk_match: insufficient credits",
            status_code=422,
            endpoint="/people/bulk_match",
        )

    client.enrich_people_bulk = enrich_people_bulk
    people = [
        Person(
            campaign_name="camp",
            country="Hungary",
            keyword="drilling",
            person_name=f"Person {index}",
            apollo_person_id=f"person-{index}",
            selected_for_enrichment=True,
            duplicate_key=f"apollo_id:person-{index}",
        )
        for index in range(1, 12)
    ]

    try:
        enrich_selected_people(people, client, db, "camp", max_total=20, progress_func=None)
    except EnrichmentStoppedError as exc:
        assert exc.partial_leads
        assert exc.partial_leads[0].email == "jane@example.com"
        assert isinstance(exc.original_error, ApolloAPIError)
    else:
        raise AssertionError("expected EnrichmentStoppedError")
    db.close()


def test_single_person_fallback_credit_error_stops_with_partial_leads(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("APOLLO_SINGLE_ENRICH_FALLBACK", "true")
    db = Database(tmp_path / "test.sqlite")
    client = object.__new__(ApolloClient)
    client.webhook_url = ""

    client.enrich_people_bulk = lambda people, campaign_name: {
        "matches": [{"id": "person-1", "email_status": "unavailable"}]
    }

    def enrich_person(person, campaign_name):
        raise ApolloAPIError(
            "Apollo API error 422 while calling /people/match: You have insufficient credits!",
            status_code=422,
            endpoint="/people/match",
            response_body='{"error":"You have insufficient credits!"}',
        )

    client.enrich_person = enrich_person
    person = Person(
        campaign_name="camp",
        country="Hungary",
        keyword="drilling",
        person_name="Jane Doe",
        apollo_person_id="person-1",
        selected_for_enrichment=True,
        duplicate_key="apollo_id:person-1",
    )

    try:
        enrich_selected_people([person], client, db, "camp", max_total=10, progress_func=None)
    except EnrichmentStoppedError as exc:
        assert len(exc.partial_leads) == 1
        assert exc.partial_leads[0].enrichment_status in {"cached_enrichment", "unavailable_after_native_and_waterfall"}
        assert exc.original_error.endpoint == "/people/match"
    else:
        raise AssertionError("expected EnrichmentStoppedError")
    db.close()
