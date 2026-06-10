from __future__ import annotations

import argparse
import logging
import math
import os
import sys

from dotenv import load_dotenv

from .apollo_client import ApolloAPIError, ApolloClient
from .campaign_state import (
    build_status_report,
    prepare_campaign_run,
    repair_campaign_state,
)
from .company_search import search_companies
from .config_loader import load_campaign_config
from .credit_guard import CreditRiskAssessment, format_credit_after_report, require_credit_approval
from .database import Database
from .doctor import run_doctor
from .enrichment import EnrichmentStoppedError, enrich_selected_people, people_needing_enrichment
from .error_handling import explain_exception, log_unexpected_exception
from .exporter import export_enriched, export_previews
from .final_export import rebuild_final_export
from .people_search import search_people_for_companies
from .utils import configure_logging
from .webhook_importer import import_webhook_payload_objects, import_webhook_payloads
from .webhook_site import collect_webhook_site_payloads


logger = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Apollo lead finding CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate", help="Validate campaign config")
    validate.add_argument("--config", required=True)

    doctor = subparsers.add_parser("doctor", help="Check config and .env without Apollo API calls")
    doctor.add_argument("--config", required=True)

    run = subparsers.add_parser("run", help="Run campaign")
    run.add_argument("--config", required=True)
    run.add_argument("--dry-run", action="store_true")
    run.add_argument("--resume", action="store_true")
    run.add_argument("--enrich-selected", action="store_true")
    run.add_argument(
        "--retry-unavailable-enrichments",
        action="store_true",
        help="Spend credits retrying people already enriched before with unavailable/no email results",
    )

    import_webhooks = subparsers.add_parser("import-webhooks", help="Import Apollo webhook.site JSON payloads")
    import_webhooks.add_argument("--path", required=True)

    collect_webhooks = subparsers.add_parser("collect-webhooks", help="Collect Apollo webhook payloads from webhook.site")
    collect_webhooks.add_argument("--config", required=False, help="Optional config for rebuilding final Excel after import")
    collect_webhooks.add_argument("--wait-seconds", type=float, default=0)

    export_final = subparsers.add_parser("export-final", help="Rebuild final enriched Excel from local cache")
    export_final.add_argument("--config", required=True)

    repair = subparsers.add_parser("repair", help="Repair local campaign state and rebuild final export without Apollo API calls")
    repair.add_argument("--config", required=True)
    repair.add_argument("--wait-seconds", type=float, default=0)

    status = subparsers.add_parser("status", help="Show campaign DB/cache/output health without changing state")
    status.add_argument("--config", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    load_dotenv()
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "import-webhooks":
        db = Database()
        try:
            summary = import_webhook_payloads(args.path, db)
        except Exception as exc:
            log_unexpected_exception(logger, exc)
            print(explain_exception(exc, "Webhook import").format(), file=sys.stderr)
            return 1
        finally:
            db.close()
        print("Webhook import complete.")
        print(f"Files read: {summary.files_read}")
        print(f"New webhook events stored: {summary.events_imported}")
        print(f"Webhook records seen: {summary.records_seen}")
        print(f"Records with email: {summary.records_with_email}")
        print(f"Enrichment cache rows updated: {summary.enrichments_updated}")
        print("No Apollo API calls were made.")
        return 0

    if args.command == "doctor":
        result = run_doctor(args.config)
        for line in result.lines:
            print(line)
        return 0 if result.ok else 2

    if args.command == "collect-webhooks":
        webhook_url = os.getenv("APOLLO_WEBHOOK_URL", "").strip()
        if not webhook_url:
            print("APOLLO_WEBHOOK_URL is missing in .env.", file=sys.stderr)
            return 2
        db = Database()
        try:
            payloads = collect_webhook_site_payloads(
                webhook_url,
                wait_seconds=args.wait_seconds,
                progress_func=lambda state: print(
                    "Webhook.site collection progress: "
                    f"payloads={state['payloads']}, seconds_remaining={state['seconds_remaining']}"
                ),
            )
            summary = import_webhook_payload_objects(payloads, db, source="webhook.site")
            print("Webhook.site collection complete.")
            print(f"Webhook payloads collected: {len(payloads)}")
            print(f"Webhook records seen: {summary.records_seen}")
            print(f"Records with email: {summary.records_with_email}")
            print(f"Enrichment cache rows updated: {summary.enrichments_updated}")
            if args.config:
                config = load_campaign_config(args.config)
                path, leads = rebuild_final_export(config, db)
                print(f"Final enriched export rebuilt: {path}")
                print(f"Rows exported: {len(leads)}")
                print(f"Rows with email: {sum(1 for lead in leads if lead.email)}")
        except Exception as exc:
            log_unexpected_exception(logger, exc)
            print(explain_exception(exc, "Webhook.site collection").format(), file=sys.stderr)
            return 1
        finally:
            db.close()
        print("No Apollo API calls were made.")
        return 0

    try:
        config = load_campaign_config(args.config)
    except Exception as exc:
        print(f"Config validation failed: {exc}", file=sys.stderr)
        return 2

    if args.command == "validate":
        print(f"Config OK: {config.campaign_name}")
        print(f"Countries: {len(config.countries)}")
        print(f"Keywords: {len(config.keywords)}")
        print(f"Organization searches planned: {len(config.countries) * len(config.keywords)}")
        return 0

    db = Database()
    db.upsert_campaign(config.campaign_name, config.model_dump())

    if args.command == "export-final":
        try:
            repair = repair_campaign_state(config, db, collect_webhooks=True, wait_seconds=0)
        finally:
            db.close()
        for line in repair.outcome_lines():
            print(line)
        print("No Apollo API calls were made.")
        return 0

    if args.command == "repair":
        try:
            repair = repair_campaign_state(config, db, collect_webhooks=True, wait_seconds=args.wait_seconds)
        finally:
            db.close()
        for line in repair.outcome_lines():
            print(line)
        print("Repair finished. No Apollo API calls were made.")
        return 0

    if args.command == "status":
        try:
            report = build_status_report(config, db)
        finally:
            db.close()
        for line in report.lines():
            print(line)
        return 0

    if args.dry_run:
        pages_per_search = math.ceil(
            config.limits.max_companies_per_keyword / ApolloClient.MAX_ORGANIZATION_SEARCH_PAGE_SIZE
        )
        print(f"Dry run OK: {config.campaign_name}")
        print(f"Organization searches planned: {len(config.countries) * len(config.keywords)}")
        print(f"Max paginated Organization Search API calls: {len(config.countries) * len(config.keywords) * pages_per_search}")
        print(f"Apollo Organization Search page size used by CLI: {ApolloClient.MAX_ORGANIZATION_SEARCH_PAGE_SIZE}")
        print(f"Max companies per keyword: {config.limits.max_companies_per_keyword}")
        print(f"Max people per company: {config.limits.max_people_per_company}")
        print("No Apollo API calls were made.")
        db.close()
        return 0

    doctor_result = run_doctor(args.config)
    print("Doctor Checks")
    for line in doctor_result.lines:
        print(line)
    if not doctor_result.ok:
        db.close()
        return 2

    run_context = prepare_campaign_run(config, db, resume=args.resume)
    planned_org_calls = len(config.countries) * len(config.keywords) * math.ceil(
        config.limits.max_companies_per_keyword / ApolloClient.MAX_ORGANIZATION_SEARCH_PAGE_SIZE
    )
    for line in run_context.lines(planned_org_calls, config.limits.max_total_people_to_enrich):
        print(line)

    client = ApolloClient(db)
    try:
        try:
            client.validate_api_key()
        except Exception as exc:
            db.update_campaign_run(run_context.run_id, status="failed", notes=str(exc), finish=True)
            print(explain_exception(exc, "Apollo API key validation").format(), file=sys.stderr)
            return 2
        org_assessment = _organization_search_risk(config, client, args.resume)
        org_balance_before = client.get_credit_balance(config.campaign_name)
        if org_assessment.may_use_credits:
            approved = require_credit_approval(org_assessment, org_balance_before)
            if not approved:
                db.update_campaign_run(run_context.run_id, status="cancelled", notes="cancelled before organization search", finish=True)
                print("Campaign cancelled before Organization Search. No Apollo search calls were made.")
                return 0
        try:
            companies = search_companies(config, client, db, resume=args.resume)
        except ApolloAPIError as exc:
            logger.error("Organization Search failed: %s", exc)
            db.update_campaign_run(run_context.run_id, status="failed", notes=str(exc), finish=True)
            print(explain_exception(exc, "Organization Search").format(), file=sys.stderr)
            print("No preview was exported because companies could not be loaded.")
            return 1
        except Exception as exc:
            log_unexpected_exception(logger, exc)
            db.update_campaign_run(run_context.run_id, status="failed", notes=str(exc), finish=True)
            print(explain_exception(exc, "Organization Search").format(), file=sys.stderr)
            print("No preview was exported because companies could not be loaded.")
            return 1
        org_balance_after = client.get_credit_balance(config.campaign_name)
        print(
            format_credit_after_report(
                "Organization Search",
                org_balance_before,
                org_balance_after,
                success_count=org_assessment.record_count,
                skipped_count=org_assessment.cached_or_skipped_count,
            )
        )
        try:
            people = search_people_for_companies(config, companies, client, db, resume=args.resume)
        except ApolloAPIError as exc:
            logger.error("People Search failed after companies were found: %s", exc)
            people = []
            print(explain_exception(exc, "People Search").format(), file=sys.stderr)
            print("Exporting company preview only so the successful Organization Search results are not lost.")
        except Exception as exc:
            log_unexpected_exception(logger, exc)
            people = []
            print(explain_exception(exc, "People Search").format(), file=sys.stderr)
            print("Exporting company preview only so the successful Organization Search results are not lost.")
        preview_paths = export_previews(companies, people, config.campaign_name)
        db.update_campaign_run(
            run_context.run_id,
            company_count=len(companies),
            people_count=len(people),
            selected_count=sum(1 for p in people if p.selected_for_enrichment),
        )
        print(f"Exported {len(companies)} companies and {len(people)} people.")
        print(f"Companies preview: {preview_paths['companies']}")
        print(f"People preview: {preview_paths['people']}")
        print(f"Campaign summary: {preview_paths['summary']}")

        if args.enrich_selected:
            selected_total = sum(1 for p in people if p.selected_for_enrichment)
            candidates = people_needing_enrichment(
                people,
                db,
                config.limits.max_total_people_to_enrich,
                retry_unavailable=args.retry_unavailable_enrichments,
            )
            skipped_before = selected_total - len(candidates)
            enrich_assessment = CreditRiskAssessment(
                operation_name="Bulk People Enrichment",
                record_count=len(candidates),
                estimated_credit_risk=(
                    f"at least up to {len(candidates):,} people enrichment attempts. "
                    "Single-person fallback and waterfall can consume additional lead/email credits."
                ),
                configured_limit=config.limits.max_total_people_to_enrich,
                cached_or_skipped_count=skipped_before,
            )
            enrich_balance_before = client.get_credit_balance(config.campaign_name)
            approved = require_credit_approval(enrich_assessment, enrich_balance_before)
            if not approved:
                repair = repair_campaign_state(config, db, collect_webhooks=True, wait_seconds=0)
                db.update_campaign_run(
                    run_context.run_id,
                    status="partial",
                    email_count=sum(1 for lead in repair.leads if lead.email),
                    notes="enrichment cancelled by user",
                    finish=True,
                )
                for line in repair.outcome_lines():
                    print(line)
                print("Enrichment cancelled. Preview files remain available.")
                return 0
            try:
                enriched = enrich_selected_people(
                    people,
                    client,
                    db,
                    config.campaign_name,
                    config.limits.max_total_people_to_enrich,
                    retry_unavailable=args.retry_unavailable_enrichments,
                    run_id=run_context.run_id,
                )
            except EnrichmentStoppedError as exc:
                logger.error("Bulk People Enrichment stopped: %s", exc)
                enriched_path = export_enriched(exc.partial_leads, config.campaign_name)
                repair = repair_campaign_state(config, db, collect_webhooks=True, wait_seconds=0)
                db.update_campaign_run(
                    run_context.run_id,
                    status="partial",
                    email_count=sum(1 for lead in repair.leads if lead.email),
                    notes=str(exc.original_error),
                    finish=True,
                )
                print(explain_exception(exc.original_error, "Bulk People Enrichment").format(), file=sys.stderr)
                print(f"Partial enriched leads exported: {enriched_path}")
                for line in repair.outcome_lines():
                    print(line)
                print(
                    "The run stopped cleanly after saving completed enrichments. "
                    "Rerun with --resume --enrich-selected after adding Apollo lead/email credits or lowering max_total_people_to_enrich."
                )
                return 1
            except Exception as exc:
                if isinstance(exc, ApolloAPIError):
                    logger.error("Bulk People Enrichment failed: %s", exc)
                else:
                    log_unexpected_exception(logger, exc)
                print(explain_exception(exc, "Bulk People Enrichment").format(), file=sys.stderr)
                print("Preview files remain available. Rerun with --resume --enrich-selected after fixing the issue.")
                return 1
            enrich_balance_after = client.get_credit_balance(config.campaign_name)
            enriched_path = export_enriched(enriched, config.campaign_name)
            print(f"Enriched leads exported: {enriched_path}")
            print(
                format_credit_after_report(
                    "Bulk People Enrichment",
                    enrich_balance_before,
                    enrich_balance_after,
                    success_count=len(enriched),
                    skipped_count=skipped_before,
                    failed_count=max(len(candidates) - len(enriched), 0),
                )
            )
        else:
            print("Enrichment skipped. Re-run with --enrich-selected to enrich selected people.")
        repair = repair_campaign_state(config, db, collect_webhooks=True, wait_seconds=0)
        db.update_campaign_run(
            run_context.run_id,
            status="success",
            email_count=sum(1 for lead in repair.leads if lead.email),
            notes="completed",
            finish=True,
        )
        for line in repair.outcome_lines():
            print(line)
        return 0
    finally:
        db.close()


def _organization_search_risk(config, client: ApolloClient, resume: bool) -> CreditRiskAssessment:
    pages_per_search = math.ceil(config.limits.max_companies_per_keyword / ApolloClient.MAX_ORGANIZATION_SEARCH_PAGE_SIZE)
    total = len(config.countries) * len(config.keywords) * pages_per_search
    cached = 0
    if resume:
        for country in config.countries:
            for keyword in config.keywords:
                for page in range(1, pages_per_search + 1):
                    if client.is_organization_search_cached(country, keyword, config.limits.max_companies_per_keyword, page):
                        cached += 1
    uncached = total - cached
    return CreditRiskAssessment(
        operation_name="Organization Search",
        record_count=uncached,
        estimated_credit_risk=(
            f"up to {uncached:,} paginated organization search API calls. "
            "Apollo says Organization Search consumes credits."
        ),
        configured_limit=config.limits.max_total_companies,
        cached_or_skipped_count=cached,
    )


if __name__ == "__main__":
    raise SystemExit(main())
