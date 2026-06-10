from pathlib import Path

import pandas as pd

from src.campaign_state import build_status_report, prepare_campaign_run, repair_campaign_state
from src.database import Database
from src.exporter import export_previews
from src.models import CampaignConfig, Limits, Person, TitleConfig


def _config(keyword: str = "drilling") -> CampaignConfig:
    return CampaignConfig(
        campaign_name="camp",
        countries=["Poland"],
        keywords=[keyword],
        limits=Limits(max_total_people_to_enrich=100),
        titles=TitleConfig(tier_1=["CEO"]),
    )


def _person(person_id: str, keyword: str = "drilling") -> Person:
    return Person(
        campaign_name="camp",
        country="Poland",
        keyword=keyword,
        person_name=f"Person {person_id}",
        apollo_person_id=person_id,
        ranking_score=100,
        selected_for_enrichment=True,
        duplicate_key=f"apollo_id:{person_id}",
    )


def test_changed_config_starts_new_run_and_archives_active_rows(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("src.utils.OUTPUT_DIR", tmp_path / "output")
    db = Database(tmp_path / "test.sqlite")
    first = _config("old")
    first_run = prepare_campaign_run(first, db, resume=False)
    stale = _person("old-1", keyword="old")
    db.upsert_person("camp", stale.duplicate_key, stale.model_dump() | {"duplicate_key": stale.duplicate_key})

    second = _config("new")
    second_run = prepare_campaign_run(second, db, resume=False)

    assert first_run.run_id != second_run.run_id
    assert second_run.config_changed is True
    assert second_run.archived_people == 1
    assert db.count_people("camp", active_only=True) == 0
    assert db.count_people("camp", active_only=False) == 1
    db.close()


def test_repair_syncs_active_people_to_preview_and_final_export(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("src.utils.OUTPUT_DIR", tmp_path / "output")
    db = Database(tmp_path / "test.sqlite")
    config = _config()
    prepare_campaign_run(config, db, resume=False)
    stale = _person("old-1")
    current = _person("current-1")
    db.upsert_person("camp", stale.duplicate_key, stale.model_dump() | {"duplicate_key": stale.duplicate_key})
    db.upsert_person("camp", current.duplicate_key, current.model_dump() | {"duplicate_key": current.duplicate_key})
    db.save_enrichment(current.duplicate_key, {"id": "current-1", "email": "current@example.com"})
    export_previews([], [current], "camp")

    result = repair_campaign_state(config, db, collect_webhooks=False)
    exported = pd.read_excel(result.final_path, dtype=str).fillna("")

    assert result.sync_archived == 1
    assert len(result.leads) == 1
    assert result.leads[0].apollo_person_id == "current-1"
    assert result.leads[0].email == "current@example.com"
    assert len(exported) == 1
    assert exported.loc[0, "apollo_person_id"] == "current-1"
    assert db.count_people("camp", active_only=True) == 1
    db.close()


def test_status_reports_stale_active_rows_vs_preview(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("src.utils.OUTPUT_DIR", tmp_path / "output")
    db = Database(tmp_path / "test.sqlite")
    config = _config()
    prepare_campaign_run(config, db, resume=False)
    db.upsert_person("camp", "apollo_id:old-1", _person("old-1").model_dump() | {"duplicate_key": "apollo_id:old-1"})
    export_previews([], [], "camp")

    report = build_status_report(config, db)
    joined = "\n".join(report.lines())

    assert report.active_people == 1
    assert report.preview_people == 0
    assert "stale_vs_preview=1" in joined
    db.close()
