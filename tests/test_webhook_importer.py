import json
from pathlib import Path

import pandas as pd

from src.database import Database
from src.final_export import rebuild_final_export
from src.exporter import export_previews
from src.models import CampaignConfig, Limits, Person, TitleConfig
from src.webhook_importer import import_webhook_payloads
from src.webhook_importer import import_webhook_payload_objects
from src.webhook_site import _payload_from_request_item, collect_webhook_site_payloads, is_webhook_site_url


def test_webhook_json_import_updates_unavailable_enrichment(tmp_path: Path) -> None:
    db = Database(tmp_path / "test.sqlite")
    db.save_enrichment("apollo_id:person-1", {"id": "person-1", "email_status": "unavailable"})
    payload_path = tmp_path / "webhook.json"
    payload_path.write_text(
        json.dumps(
            {
                "matches": [
                    {
                        "person": {
                            "id": "person-1",
                            "name": "Jane Doe",
                            "waterfall_email": "jane@example.com",
                            "email_status": "verified",
                        }
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    summary = import_webhook_payloads(payload_path, db)
    payload = db.get_enrichment("apollo_id:person-1")

    assert summary.files_read == 1
    assert summary.records_with_email == 1
    assert summary.enrichments_updated == 1
    assert payload is not None
    assert payload["person"]["waterfall_email"] == "jane@example.com"
    assert db.enrichment_has_usable_email("apollo_id:person-1")
    db.close()


def test_webhook_import_handles_webhook_site_body_wrapper(tmp_path: Path) -> None:
    db = Database(tmp_path / "test.sqlite")
    person = Person(
        campaign_name="camp",
        country="Hungary",
        keyword="drilling",
        person_name="Jane Doe",
        apollo_person_id="person-1",
        selected_for_enrichment=True,
        duplicate_key="apollo_id:person-1",
    )
    db.upsert_person("camp", person.duplicate_key, person.model_dump() | {"duplicate_key": person.duplicate_key})
    payload_path = tmp_path / "webhook_site_export.json"
    payload_path.write_text(
        json.dumps(
            {
                "uuid": "webhook-site-request-1",
                "content": json.dumps({"people": [{"id": "person-1", "email": "jane@example.com"}]}),
            }
        ),
        encoding="utf-8",
    )

    summary = import_webhook_payloads(payload_path, db)

    assert summary.enrichments_updated == 1
    assert db.get_enrichment("apollo_id:person-1")["waterfall_email"] == "jane@example.com"
    db.close()


def test_rebuild_final_export_uses_local_cache_without_internal_columns(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("src.utils.OUTPUT_DIR", tmp_path)
    db = Database(tmp_path / "test.sqlite")
    config = CampaignConfig(
        campaign_name="camp",
        countries=["Hungary"],
        keywords=["drilling"],
        limits=Limits(max_total_people_to_enrich=10),
        titles=TitleConfig(tier_1=["CEO"]),
    )
    person = Person(
        campaign_name="camp",
        country="Hungary",
        keyword="drilling",
        company_name="Acme",
        person_name="Jane Do***",
        first_name="Jane",
        last_name="Do***",
        title="CEO",
        apollo_person_id="person-1",
        ranking_score=100,
        selected_for_enrichment=True,
        duplicate_key="apollo_id:person-1",
    )
    db.upsert_person("camp", person.duplicate_key, person.model_dump() | {"duplicate_key": person.duplicate_key})
    db.save_enrichment(
        person.duplicate_key,
        {
            "person": {
                "id": "person-1",
                "name": "Jane Doe",
                "first_name": "Jane",
                "last_name": "Doe",
                "linkedin_url": "https://linkedin.com/in/jane-doe",
                "waterfall_email": "jane@example.com",
            }
        },
    )

    path, leads = rebuild_final_export(config, db)
    columns = set(pd.read_excel(path).columns)

    assert path.exists()
    assert leads[0].person_name == "Jane Doe"
    assert leads[0].email == "jane@example.com"
    assert "selected_for_enrichment" not in columns
    assert "already_enriched_before" not in columns
    assert "apollo_credit_risk_note" not in columns
    db.close()


def test_rebuild_final_export_prefers_latest_people_preview_over_stale_db(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("src.utils.OUTPUT_DIR", tmp_path)
    db = Database(tmp_path / "test.sqlite")
    config = CampaignConfig(
        campaign_name="camp",
        countries=["Poland"],
        keywords=["drilling"],
        limits=Limits(max_total_people_to_enrich=100),
        titles=TitleConfig(tier_1=["CEO"]),
    )
    stale = Person(
        campaign_name="camp",
        country="Poland",
        keyword="old",
        person_name="Old Person",
        apollo_person_id="old-1",
        ranking_score=100,
        selected_for_enrichment=True,
        duplicate_key="apollo_id:old-1",
    )
    current = Person(
        campaign_name="camp",
        country="Poland",
        keyword="new",
        person_name="Current Person",
        apollo_person_id="current-1",
        ranking_score=100,
        selected_for_enrichment=True,
        duplicate_key="apollo_id:current-1",
    )
    db.upsert_person("camp", stale.duplicate_key, stale.model_dump() | {"duplicate_key": stale.duplicate_key})
    export_previews([], [current], "camp")

    path, leads = rebuild_final_export(config, db)

    assert path.exists()
    assert len(leads) == 1
    assert leads[0].apollo_person_id == "current-1"
    db.close()


def test_import_apollo_waterfall_emails_list_shape(tmp_path: Path) -> None:
    db = Database(tmp_path / "test.sqlite")
    db.save_enrichment("apollo_id:person-1", {"id": "person-1", "email_status": "unavailable"})

    summary = import_webhook_payload_objects(
        [
            {
                "request_id": "req-1",
                "people": [
                    {
                        "id": "person-1",
                        "emails": [{"email": "jane@example.com"}],
                        "waterfall": {"emails": [{"vendors": [{"emails": ["jane@example.com"]}]}]},
                    }
                ],
            }
        ],
        db,
        source="test",
    )

    assert summary.records_with_email == 1
    assert db.enrichment_has_usable_email("apollo_id:person-1")
    assert db.get_enrichment("apollo_id:person-1")["waterfall_email"] == "jane@example.com"
    db.close()


def test_webhook_site_request_content_is_unwrapped() -> None:
    item = {
        "uuid": "event-1",
        "created_at": "2026-06-10 10:00:00",
        "content": json.dumps({"request_id": "req-1", "people": [{"id": "person-1"}]}),
    }

    payload = _payload_from_request_item(item)

    assert payload["request_id"] == "req-1"
    assert payload["_webhook_event_key"] == "event-1"
    assert is_webhook_site_url("https://webhook.site/7491b875-9742-4822-94c1-f7b37e81ab2a")


def test_webhook_site_collection_reports_progress(monkeypatch) -> None:
    states = []

    monkeypatch.setattr(
        "src.webhook_site._fetch_webhook_site_payloads",
        lambda token, per_page=100, max_pages=10: [{"request_id": "req-1", "_webhook_event_key": "event-1"}],
    )

    payloads = collect_webhook_site_payloads(
        "https://webhook.site/7491b875-9742-4822-94c1-f7b37e81ab2a",
        request_ids={"req-1"},
        wait_seconds=0,
        progress_func=states.append,
    )

    assert payloads
    assert states[-1]["received"] == 1
    assert states[-1]["missing"] == 0
