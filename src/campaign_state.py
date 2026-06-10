from __future__ import annotations

import hashlib
import json
import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

from .database import Database
from .final_export import _people_payloads_from_preview, rebuild_final_export
from .models import CampaignConfig, EnrichedLead
from .utils import campaign_output_dir, utc_now_iso
from .webhook_importer import WebhookImportSummary, import_webhook_payload_objects
from .webhook_site import collect_webhook_site_payloads


@dataclass
class CampaignRunContext:
    campaign_name: str
    run_id: str
    config_hash: str
    output_folder: Path
    resumed: bool
    config_changed: bool
    active_people_before: int
    preview_people: int
    archived_people: int = 0
    archived_companies: int = 0
    action: str = ""

    def lines(self, planned_org_calls: int, planned_people_limit: int) -> list[str]:
        return [
            "State Report",
            f"Campaign: {self.campaign_name}",
            f"Current run: {self.run_id}",
            f"Config changed since last run: {'yes' if self.config_changed else 'no'}",
            f"Active people before cleanup: {self.active_people_before}",
            f"Latest preview people: {self.preview_people}",
            f"Action: {self.action}",
            f"Apollo calls planned: up to {planned_org_calls} organization search calls, {planned_people_limit} people enrichment limit",
            f"Output folder: {self.output_folder}",
        ]


@dataclass
class RepairResult:
    final_path: Path
    leads: list[EnrichedLead]
    preview_people: int
    active_people: int
    sync_archived: int = 0
    webhook_summary: WebhookImportSummary = field(default_factory=WebhookImportSummary)
    webhook_error: str = ""

    def outcome_lines(self) -> list[str]:
        email_count = sum(1 for lead in self.leads if lead.email)
        attempted_no_email = sum(
            1
            for lead in self.leads
            if not lead.email and lead.enrichment_status not in {"", "not_enriched"}
        )
        never_enriched = sum(1 for lead in self.leads if lead.enrichment_status in {"", "not_enriched"})
        lines = [
            "Outcome Report",
            f"Preview rows: {self.preview_people}",
            f"Final rows: {len(self.leads)}",
            f"Emails found: {email_count}",
            f"Already attempted/no email: {attempted_no_email}",
            f"Never enriched: {never_enriched}",
            (
                "Webhook sync: "
                f"collected {self.webhook_summary.files_read} payloads, "
                f"{self.webhook_summary.records_seen} records, "
                f"{self.webhook_summary.records_with_email} records with email, "
                f"{self.webhook_summary.enrichments_updated} cache updates"
            ),
            f"Archived stale active people: {self.sync_archived}",
            f"Final export: {self.final_path}",
        ]
        if self.webhook_error:
            lines.append(f"Webhook sync warning: {self.webhook_error}")
        return lines


@dataclass
class StatusReport:
    campaign_name: str
    latest_run: dict[str, Any] | None
    output_folder: Path
    active_companies: int
    total_companies: int
    active_people: int
    total_people: int
    preview_people: int
    final_people: int
    final_emails: int
    enrichment_rows: int
    webhook_events: int

    def lines(self) -> list[str]:
        run_text = "none"
        if self.latest_run:
            run_text = (
                f"{self.latest_run['run_id']} "
                f"status={self.latest_run['status']} "
                f"started={self.latest_run['started_at']}"
            )
        stale_people = max(self.active_people - self.preview_people, 0)
        return [
            "Status Report",
            f"Campaign: {self.campaign_name}",
            f"Latest run: {run_text}",
            f"Output folder: {self.output_folder}",
            f"Companies: active={self.active_companies}, total_history={self.total_companies}",
            f"People: active={self.active_people}, total_history={self.total_people}, stale_vs_preview={stale_people}",
            f"Preview rows: {self.preview_people}",
            f"Final rows: {self.final_people}",
            f"Final emails: {self.final_emails}",
            f"Enrichment cache rows: {self.enrichment_rows}",
            f"Webhook events stored: {self.webhook_events}",
            "No Apollo API calls were made.",
        ]


def campaign_config_hash(config: CampaignConfig) -> str:
    canonical = json.dumps(config.model_dump(), sort_keys=True, ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def prepare_campaign_run(config: CampaignConfig, db: Database, resume: bool = False) -> CampaignRunContext:
    output_folder = campaign_output_dir(config.campaign_name)
    config_hash = campaign_config_hash(config)
    latest = db.get_latest_campaign_run(config.campaign_name)
    config_changed = bool(latest and latest.get("config_hash") != config_hash)
    active_people_before = db.count_people(config.campaign_name, active_only=True)
    preview_people = count_people_preview(config.campaign_name)

    can_resume = (
        resume
        and latest is not None
        and not config_changed
        and latest.get("status") in {"running", "partial"}
    )
    if can_resume:
        run_id = str(latest["run_id"])
        db.set_current_run(run_id)
        db.update_campaign_run(run_id, status="running", notes="resumed")
        return CampaignRunContext(
            campaign_name=config.campaign_name,
            run_id=run_id,
            config_hash=config_hash,
            output_folder=output_folder,
            resumed=True,
            config_changed=False,
            active_people_before=active_people_before,
            preview_people=preview_people,
            action="resuming latest compatible run",
        )

    archived = {"companies": 0, "people": 0}
    if active_people_before or db.count_companies(config.campaign_name, active_only=True):
        archived = db.archive_campaign_search_results(config.campaign_name)
    run_id = f"{utc_now_iso()}-{uuid.uuid4().hex[:8]}"
    notes = "fresh run"
    if config_changed:
        notes = "fresh run; config changed since previous run"
    elif resume and latest:
        notes = "fresh run; latest run was already finished"
    db.create_campaign_run(
        run_id=run_id,
        campaign_name=config.campaign_name,
        config_hash=config_hash,
        output_folder=str(output_folder),
        notes=notes,
    )
    action = "starting fresh run"
    if archived["people"] or archived["companies"]:
        action += f"; archived {archived['people']} people and {archived['companies']} companies"
    return CampaignRunContext(
        campaign_name=config.campaign_name,
        run_id=run_id,
        config_hash=config_hash,
        output_folder=output_folder,
        resumed=False,
        config_changed=config_changed,
        active_people_before=active_people_before,
        preview_people=preview_people,
        archived_people=archived["people"],
        archived_companies=archived["companies"],
        action=action,
    )


def repair_campaign_state(
    config: CampaignConfig,
    db: Database,
    collect_webhooks: bool = True,
    wait_seconds: float = 0,
    progress_func=print,
) -> RepairResult:
    latest = db.get_latest_campaign_run(config.campaign_name)
    created_repair_run = False
    if latest is None:
        run_id = f"{utc_now_iso()}-{uuid.uuid4().hex[:8]}"
        db.create_campaign_run(
            run_id=run_id,
            campaign_name=config.campaign_name,
            config_hash=campaign_config_hash(config),
            output_folder=str(campaign_output_dir(config.campaign_name)),
            notes="local repair run created from existing output/cache",
        )
        latest = db.get_latest_campaign_run(config.campaign_name)
        created_repair_run = True
    if latest:
        db.set_current_run(str(latest["run_id"]))
    sync = sync_active_people_from_preview(config, db)
    webhook_summary = WebhookImportSummary()
    webhook_error = ""
    if collect_webhooks:
        webhook_summary, webhook_error = collect_configured_webhooks(
            db,
            run_id=str(latest["run_id"]) if latest else None,
            wait_seconds=wait_seconds,
            progress_func=progress_func,
        )
    final_path, leads = rebuild_final_export(config, db)
    active_people = db.count_people(config.campaign_name, active_only=True)
    if latest:
        db.update_campaign_run(
            str(latest["run_id"]),
            status="repaired" if created_repair_run else None,
            people_count=active_people,
            selected_count=len(leads),
            email_count=sum(1 for lead in leads if lead.email),
            finish=created_repair_run,
        )
    return RepairResult(
        final_path=final_path,
        leads=leads,
        preview_people=count_people_preview(config.campaign_name),
        active_people=active_people,
        sync_archived=sync.get("archived", 0),
        webhook_summary=webhook_summary,
        webhook_error=webhook_error,
    )


def collect_configured_webhooks(
    db: Database,
    run_id: str | None = None,
    wait_seconds: float = 0,
    progress_func=print,
) -> tuple[WebhookImportSummary, str]:
    webhook_url = os.getenv("APOLLO_WEBHOOK_URL", "").strip()
    if not webhook_url:
        return WebhookImportSummary(), ""
    try:
        payloads = collect_webhook_site_payloads(
            webhook_url,
            wait_seconds=wait_seconds,
            progress_func=(
                lambda state: progress_func(
                    "Webhook sync progress: "
                    f"payloads={state['payloads']}, "
                    f"seconds_remaining={state['seconds_remaining']}"
                )
                if progress_func
                else None
            ),
        )
    except Exception as exc:
        return WebhookImportSummary(), str(exc)
    return import_webhook_payload_objects(payloads, db, source="webhook.site", run_id=run_id), ""


def sync_active_people_from_preview(config: CampaignConfig, db: Database) -> dict[str, int]:
    preview_path = campaign_output_dir(config.campaign_name) / "people_preview.xlsx"
    if not preview_path.exists():
        return {"active_before": db.count_people(config.campaign_name, active_only=True), "activated": 0, "archived": 0}
    payloads = _people_payloads_from_preview(preview_path, db)
    keys = {str(payload["duplicate_key"]) for payload in payloads if payload.get("duplicate_key")}
    latest = db.get_latest_campaign_run(config.campaign_name)
    return db.sync_active_people_to_keys(
        config.campaign_name,
        keys,
        run_id=str(latest["run_id"]) if latest else None,
    )


def build_status_report(config: CampaignConfig, db: Database) -> StatusReport:
    final_path = campaign_output_dir(config.campaign_name) / "final_leads_enriched.xlsx"
    final_people, final_emails = _final_counts(final_path)
    enrichment_rows = db.conn.execute("SELECT COUNT(*) AS count FROM enrichments").fetchone()["count"]
    webhook_events = db.conn.execute("SELECT COUNT(*) AS count FROM webhook_events").fetchone()["count"]
    return StatusReport(
        campaign_name=config.campaign_name,
        latest_run=db.get_latest_campaign_run(config.campaign_name),
        output_folder=campaign_output_dir(config.campaign_name),
        active_companies=db.count_companies(config.campaign_name, active_only=True),
        total_companies=db.count_companies(config.campaign_name, active_only=False),
        active_people=db.count_people(config.campaign_name, active_only=True),
        total_people=db.count_people(config.campaign_name, active_only=False),
        preview_people=count_people_preview(config.campaign_name),
        final_people=final_people,
        final_emails=final_emails,
        enrichment_rows=int(enrichment_rows),
        webhook_events=int(webhook_events),
    )


def count_people_preview(campaign_name: str) -> int:
    path = campaign_output_dir(campaign_name) / "people_preview.xlsx"
    if not path.exists():
        return 0
    try:
        return int(len(pd.read_excel(path)))
    except Exception:
        return 0


def _final_counts(path: Path) -> tuple[int, int]:
    if not path.exists():
        return 0, 0
    try:
        df = pd.read_excel(path, dtype=str).fillna("")
    except Exception:
        return 0, 0
    email_count = int((df.get("email", pd.Series(dtype=str)).astype(str).str.strip() != "").sum())
    return int(len(df)), email_count
