from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .utils import DB_PATH, ensure_dirs, utc_now_iso


class Database:
    def __init__(self, path: str | Path = DB_PATH) -> None:
        ensure_dirs()
        self.path = Path(path)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.current_run_id: str | None = None
        self.init()

    def init(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS campaigns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                campaign_name TEXT UNIQUE NOT NULL,
                config_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS companies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                campaign_name TEXT NOT NULL,
                duplicate_key TEXT NOT NULL,
                apollo_organization_id TEXT,
                domain TEXT,
                linkedin_url TEXT,
                company_name TEXT,
                country TEXT,
                keyword TEXT,
                score INTEGER,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(campaign_name, duplicate_key)
            );
            CREATE TABLE IF NOT EXISTS people (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                campaign_name TEXT NOT NULL,
                duplicate_key TEXT NOT NULL,
                apollo_person_id TEXT,
                linkedin_url TEXT,
                person_name TEXT,
                company_domain TEXT,
                selected_for_enrichment INTEGER DEFAULT 0,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(campaign_name, duplicate_key)
            );
            CREATE TABLE IF NOT EXISTS api_calls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                method TEXT NOT NULL,
                endpoint TEXT NOT NULL,
                request_hash TEXT,
                status_code INTEGER,
                cache_key TEXT,
                campaign_name TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS enrichments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                enrichment_key TEXT UNIQUE NOT NULL,
                apollo_person_id TEXT,
                linkedin_url TEXT,
                email TEXT,
                payload_json TEXT NOT NULL,
                enriched_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS search_cache (
                cache_key TEXT PRIMARY KEY,
                endpoint TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS webhook_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_key TEXT UNIQUE NOT NULL,
                payload_json TEXT NOT NULL,
                imported_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS campaign_runs (
                run_id TEXT PRIMARY KEY,
                campaign_name TEXT NOT NULL,
                config_hash TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                status TEXT NOT NULL,
                output_folder TEXT NOT NULL,
                company_count INTEGER DEFAULT 0,
                people_count INTEGER DEFAULT 0,
                selected_count INTEGER DEFAULT 0,
                email_count INTEGER DEFAULT 0,
                notes TEXT DEFAULT ''
            );
            """
        )
        self._migrate_columns()
        self.conn.commit()

    def _migrate_columns(self) -> None:
        self._add_column_if_missing("companies", "run_id", "TEXT")
        self._add_column_if_missing("companies", "active", "INTEGER DEFAULT 1")
        self._add_column_if_missing("people", "run_id", "TEXT")
        self._add_column_if_missing("people", "active", "INTEGER DEFAULT 1")
        self._add_column_if_missing("webhook_events", "run_id", "TEXT")

    def _add_column_if_missing(self, table: str, column: str, definition: str) -> None:
        rows = self.conn.execute(f"PRAGMA table_info({table})").fetchall()
        if column not in {row["name"] for row in rows}:
            self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def set_current_run(self, run_id: str | None) -> None:
        self.current_run_id = run_id

    def upsert_campaign(self, campaign_name: str, config: dict[str, Any]) -> None:
        now = utc_now_iso()
        self.conn.execute(
            """
            INSERT INTO campaigns (campaign_name, config_json, created_at, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(campaign_name) DO UPDATE SET
                config_json=excluded.config_json,
                updated_at=excluded.updated_at
            """,
            (campaign_name, json.dumps(config), now, now),
        )
        self.conn.commit()

    def create_campaign_run(
        self,
        run_id: str,
        campaign_name: str,
        config_hash: str,
        output_folder: str,
        status: str = "running",
        notes: str = "",
    ) -> None:
        now = utc_now_iso()
        self.conn.execute(
            """
            INSERT INTO campaign_runs (
                run_id, campaign_name, config_hash, started_at, status, output_folder, notes
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id) DO UPDATE SET
                status=excluded.status,
                notes=excluded.notes
            """,
            (run_id, campaign_name, config_hash, now, status, output_folder, notes),
        )
        self.conn.commit()
        self.set_current_run(run_id)

    def get_latest_campaign_run(self, campaign_name: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            SELECT *
            FROM campaign_runs
            WHERE campaign_name = ?
            ORDER BY started_at DESC
            LIMIT 1
            """,
            (campaign_name,),
        ).fetchone()
        return dict(row) if row else None

    def update_campaign_run(
        self,
        run_id: str,
        status: str | None = None,
        company_count: int | None = None,
        people_count: int | None = None,
        selected_count: int | None = None,
        email_count: int | None = None,
        notes: str | None = None,
        finish: bool = False,
    ) -> None:
        assignments: list[str] = []
        values: list[Any] = []
        for column, value in (
            ("status", status),
            ("company_count", company_count),
            ("people_count", people_count),
            ("selected_count", selected_count),
            ("email_count", email_count),
            ("notes", notes),
        ):
            if value is not None:
                assignments.append(f"{column} = ?")
                values.append(value)
        if finish:
            assignments.append("finished_at = ?")
            values.append(utc_now_iso())
        if not assignments:
            return
        values.append(run_id)
        self.conn.execute(f"UPDATE campaign_runs SET {', '.join(assignments)} WHERE run_id = ?", values)
        self.conn.commit()

    def clear_campaign_search_results(self, campaign_name: str) -> None:
        self.archive_campaign_search_results(campaign_name)

    def archive_campaign_search_results(self, campaign_name: str) -> dict[str, int]:
        companies = self.conn.execute(
            "SELECT COUNT(*) AS count FROM companies WHERE campaign_name = ? AND COALESCE(active, 1) = 1",
            (campaign_name,),
        ).fetchone()["count"]
        people = self.conn.execute(
            "SELECT COUNT(*) AS count FROM people WHERE campaign_name = ? AND COALESCE(active, 1) = 1",
            (campaign_name,),
        ).fetchone()["count"]
        self.conn.execute("UPDATE companies SET active = 0, updated_at = ? WHERE campaign_name = ?", (utc_now_iso(), campaign_name))
        self.conn.execute("UPDATE people SET active = 0, updated_at = ? WHERE campaign_name = ?", (utc_now_iso(), campaign_name))
        self.conn.commit()
        return {"companies": int(companies), "people": int(people)}

    def count_people(self, campaign_name: str, active_only: bool = False, selected_only: bool = False) -> int:
        clauses = ["campaign_name = ?"]
        if active_only:
            clauses.append("COALESCE(active, 1) = 1")
        if selected_only:
            clauses.append("selected_for_enrichment = 1")
        row = self.conn.execute(
            f"SELECT COUNT(*) AS count FROM people WHERE {' AND '.join(clauses)}",
            (campaign_name,),
        ).fetchone()
        return int(row["count"])

    def count_companies(self, campaign_name: str, active_only: bool = False) -> int:
        clauses = ["campaign_name = ?"]
        if active_only:
            clauses.append("COALESCE(active, 1) = 1")
        row = self.conn.execute(
            f"SELECT COUNT(*) AS count FROM companies WHERE {' AND '.join(clauses)}",
            (campaign_name,),
        ).fetchone()
        return int(row["count"])

    def count_emails_for_campaign(self, campaign_name: str, active_only: bool = True) -> int:
        keys = [payload["duplicate_key"] for payload in self.list_selected_person_payloads(campaign_name, active_only=active_only)]
        return sum(1 for key in keys if self.enrichment_has_usable_email(key))

    def sync_active_people_to_keys(self, campaign_name: str, duplicate_keys: set[str], run_id: str | None = None) -> dict[str, int]:
        active_before = self.count_people(campaign_name, active_only=True)
        rows = self.conn.execute(
            "SELECT duplicate_key, COALESCE(active, 1) AS active FROM people WHERE campaign_name = ?",
            (campaign_name,),
        ).fetchall()
        activated = 0
        archived = 0
        now = utc_now_iso()
        for row in rows:
            key = str(row["duplicate_key"])
            should_be_active = key in duplicate_keys
            was_active = bool(row["active"])
            if should_be_active:
                self.conn.execute(
                    """
                    UPDATE people
                    SET active = 1, run_id = COALESCE(?, run_id), updated_at = ?
                    WHERE campaign_name = ? AND duplicate_key = ?
                    """,
                    (run_id, now, campaign_name, key),
                )
                if not was_active:
                    activated += 1
            else:
                self.conn.execute(
                    """
                    UPDATE people
                    SET active = 0, updated_at = ?
                    WHERE campaign_name = ? AND duplicate_key = ?
                    """,
                    (now, campaign_name, key),
                )
                if was_active:
                    archived += 1
        self.conn.commit()
        return {"active_before": active_before, "activated": activated, "archived": archived}

    def log_api_call(
        self,
        method: str,
        endpoint: str,
        status_code: int | None = None,
        request_hash: str | None = None,
        cache_key: str | None = None,
        campaign_name: str | None = None,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO api_calls (method, endpoint, request_hash, status_code, cache_key, campaign_name, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (method, endpoint, request_hash, status_code, cache_key, campaign_name, utc_now_iso()),
        )
        self.conn.commit()

    def get_cached_search(self, cache_key: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT payload_json FROM search_cache WHERE cache_key = ?", (cache_key,)).fetchone()
        return json.loads(row["payload_json"]) if row else None

    def set_cached_search(self, cache_key: str, endpoint: str, payload: dict[str, Any]) -> None:
        self.conn.execute(
            """
            INSERT INTO search_cache (cache_key, endpoint, payload_json, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(cache_key) DO UPDATE SET payload_json=excluded.payload_json
            """,
            (cache_key, endpoint, json.dumps(payload), utc_now_iso()),
        )
        self.conn.commit()

    def upsert_company(self, campaign_name: str, duplicate_key: str, payload: dict[str, Any]) -> None:
        now = utc_now_iso()
        self.conn.execute(
            """
            INSERT INTO companies (
                campaign_name, duplicate_key, apollo_organization_id, domain, linkedin_url,
                company_name, country, keyword, score, payload_json, created_at, updated_at, run_id, active
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(campaign_name, duplicate_key) DO UPDATE SET
                run_id=excluded.run_id,
                active=1,
                apollo_organization_id=excluded.apollo_organization_id,
                domain=excluded.domain,
                linkedin_url=excluded.linkedin_url,
                company_name=excluded.company_name,
                country=excluded.country,
                keyword=excluded.keyword,
                score=excluded.score,
                payload_json=excluded.payload_json,
                updated_at=excluded.updated_at
            """,
            (
                campaign_name,
                duplicate_key,
                payload.get("apollo_organization_id"),
                payload.get("domain"),
                payload.get("linkedin_url"),
                payload.get("company_name"),
                payload.get("country"),
                payload.get("keyword"),
                payload.get("company_score"),
                json.dumps(payload),
                now,
                now,
                self.current_run_id,
                1,
            ),
        )
        self.conn.commit()

    def upsert_person(self, campaign_name: str, duplicate_key: str, payload: dict[str, Any]) -> None:
        now = utc_now_iso()
        self.conn.execute(
            """
            INSERT INTO people (
                campaign_name, duplicate_key, apollo_person_id, linkedin_url, person_name,
                company_domain, selected_for_enrichment, payload_json, created_at, updated_at, run_id, active
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(campaign_name, duplicate_key) DO UPDATE SET
                run_id=excluded.run_id,
                active=1,
                apollo_person_id=excluded.apollo_person_id,
                linkedin_url=excluded.linkedin_url,
                person_name=excluded.person_name,
                company_domain=excluded.company_domain,
                selected_for_enrichment=excluded.selected_for_enrichment,
                payload_json=excluded.payload_json,
                updated_at=excluded.updated_at
            """,
            (
                campaign_name,
                duplicate_key,
                payload.get("apollo_person_id"),
                payload.get("linkedin_url"),
                payload.get("person_name"),
                payload.get("domain"),
                1 if payload.get("selected_for_enrichment") else 0,
                json.dumps(payload),
                now,
                now,
                self.current_run_id,
                1,
            ),
        )
        self.conn.commit()

    def has_enrichment(self, enrichment_key: str) -> bool:
        row = self.conn.execute("SELECT 1 FROM enrichments WHERE enrichment_key = ?", (enrichment_key,)).fetchone()
        return row is not None

    def get_enrichment(self, enrichment_key: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT payload_json FROM enrichments WHERE enrichment_key = ?", (enrichment_key,)).fetchone()
        return json.loads(row["payload_json"]) if row else None

    def list_selected_person_payloads(
        self,
        campaign_name: str,
        limit: int | None = None,
        active_only: bool = True,
        run_id: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses = ["campaign_name = ?", "selected_for_enrichment = 1"]
        params: list[Any] = [campaign_name]
        if active_only:
            clauses.append("COALESCE(active, 1) = 1")
        if run_id:
            clauses.append("run_id = ?")
            params.append(run_id)
        rows = self.conn.execute(
            f"""
            SELECT duplicate_key, selected_for_enrichment, payload_json
            FROM people
            WHERE {' AND '.join(clauses)}
            """,
            params,
        ).fetchall()
        payloads: list[dict[str, Any]] = []
        for row in rows:
            payload = json.loads(row["payload_json"])
            payload["duplicate_key"] = row["duplicate_key"]
            payload["selected_for_enrichment"] = bool(row["selected_for_enrichment"])
            payload["already_enriched_before"] = self.enrichment_has_usable_email(row["duplicate_key"])
            payloads.append(payload)
        payloads.sort(key=lambda item: int(item.get("ranking_score") or 0), reverse=True)
        return payloads[:limit] if limit is not None else payloads

    def find_enrichment_key(
        self,
        apollo_person_id: str = "",
        linkedin_url: str = "",
        fallback_key: str = "",
    ) -> str:
        if apollo_person_id:
            row = self.conn.execute(
                "SELECT enrichment_key FROM enrichments WHERE apollo_person_id = ? LIMIT 1",
                (apollo_person_id,),
            ).fetchone()
            if row:
                return str(row["enrichment_key"])
        if linkedin_url:
            row = self.conn.execute(
                "SELECT enrichment_key FROM enrichments WHERE linkedin_url = ? LIMIT 1",
                (linkedin_url,),
            ).fetchone()
            if row:
                return str(row["enrichment_key"])
        if fallback_key and self.has_enrichment(fallback_key):
            return fallback_key
        return ""

    def find_person_duplicate_key(
        self,
        apollo_person_id: str = "",
        linkedin_url: str = "",
        fallback_key: str = "",
    ) -> str:
        if apollo_person_id:
            row = self.conn.execute(
                "SELECT duplicate_key FROM people WHERE apollo_person_id = ? LIMIT 1",
                (apollo_person_id,),
            ).fetchone()
            if row:
                return str(row["duplicate_key"])
        if linkedin_url:
            row = self.conn.execute(
                "SELECT duplicate_key FROM people WHERE linkedin_url = ? LIMIT 1",
                (linkedin_url,),
            ).fetchone()
            if row:
                return str(row["duplicate_key"])
        return fallback_key

    def enrichment_has_usable_email(self, enrichment_key: str) -> bool:
        payload = self.get_enrichment(enrichment_key)
        if not payload:
            return False
        email, status = _extract_email_fields(payload)
        return bool(email) and status not in {"unavailable", "not_found", "not found", "invalid"}

    def save_enrichment(self, enrichment_key: str, payload: dict[str, Any]) -> None:
        apollo_person_id, linkedin_url = _extract_identity_fields(payload)
        self.conn.execute(
            """
            INSERT INTO enrichments (
                enrichment_key, apollo_person_id, linkedin_url, email, payload_json, enriched_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(enrichment_key) DO UPDATE SET
                apollo_person_id=excluded.apollo_person_id,
                linkedin_url=excluded.linkedin_url,
                email=excluded.email,
                payload_json=excluded.payload_json,
                enriched_at=excluded.enriched_at
            """,
            (
                enrichment_key,
                apollo_person_id,
                linkedin_url,
                _extract_email_fields(payload)[0],
                json.dumps(payload),
                utc_now_iso(),
            ),
        )
        self.conn.commit()

    def save_webhook_event(self, event_key: str, payload: dict[str, Any], run_id: str | None = None) -> bool:
        cursor = self.conn.execute(
            """
            INSERT OR IGNORE INTO webhook_events (event_key, payload_json, imported_at, run_id)
            VALUES (?, ?, ?, ?)
            """,
            (event_key, json.dumps(payload), utc_now_iso(), run_id or self.current_run_id),
        )
        self.conn.commit()
        return cursor.rowcount > 0

    def close(self) -> None:
        self.conn.close()


def _payload_candidates(payload: dict[str, Any]) -> list[dict[str, Any]]:
    candidates = [payload]
    for key in ("person", "contact", "match"):
        nested = payload.get(key)
        if isinstance(nested, dict):
            candidates.append(nested)
    return candidates


def _extract_identity_fields(payload: dict[str, Any]) -> tuple[str, str]:
    for candidate in _payload_candidates(payload):
        apollo_person_id = str(
            candidate.get("apollo_person_id") or candidate.get("person_id") or candidate.get("id") or ""
        ).strip()
        linkedin_url = str(candidate.get("linkedin_url") or "").strip()
        if apollo_person_id or linkedin_url:
            return apollo_person_id, linkedin_url
    return "", ""


def _extract_email_fields(payload: dict[str, Any]) -> tuple[str, str]:
    for candidate in _payload_candidates(payload):
        email = str(candidate.get("email") or "").strip()
        status = str(candidate.get("email_status") or candidate.get("email_verification_status") or "").strip().lower()
        if email:
            return email, status
        personal_emails = candidate.get("personal_emails") or candidate.get("personal_email")
        if isinstance(personal_emails, list) and personal_emails:
            return str(personal_emails[0] or "").strip(), status
        if isinstance(personal_emails, str) and personal_emails.strip():
            return personal_emails.strip(), status
        waterfall_email = candidate.get("waterfall_email") or candidate.get("waterfall_email_address")
        if waterfall_email:
            return str(waterfall_email).strip(), status
        email_items = candidate.get("emails")
        if isinstance(email_items, list):
            for item in email_items:
                email = item.get("email") if isinstance(item, dict) else item
                if email:
                    return str(email).strip(), status
    status = str(payload.get("email_status") or payload.get("email_verification_status") or "").strip().lower()
    return "", status
