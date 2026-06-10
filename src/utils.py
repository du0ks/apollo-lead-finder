from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


ROOT_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT_DIR / "data"
OUTPUT_DIR = DATA_DIR / "output"
LOG_DIR = ROOT_DIR / "logs"
DB_PATH = DATA_DIR / "apollo_leads.sqlite"


def ensure_dirs() -> None:
    for path in (DATA_DIR, OUTPUT_DIR, LOG_DIR):
        path.mkdir(parents=True, exist_ok=True)


def safe_path_name(value: str | None, fallback: str = "default") -> str:
    cleaned = normalize_text(value or fallback).replace(" ", "_")
    return cleaned or fallback


def campaign_output_dir(campaign_name: str) -> Path:
    path = OUTPUT_DIR / safe_path_name(campaign_name, fallback="campaign")
    path.mkdir(parents=True, exist_ok=True)
    return path


def campaign_raw_dir(campaign_name: str | None) -> Path:
    path = campaign_output_dir(campaign_name or "uncategorized") / "raw"
    path.mkdir(parents=True, exist_ok=True)
    return path


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize_text(value: str | None) -> str:
    if not value:
        return ""
    value = value.strip().lower()
    value = re.sub(r"https?://", "", value)
    value = re.sub(r"^www\.", "", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def normalize_domain(value: str | None) -> str:
    if not value:
        return ""
    raw = value.strip().lower()
    if not raw:
        return ""
    parsed = urlparse(raw if "://" in raw else f"https://{raw}")
    host = parsed.netloc or parsed.path
    host = host.split("/")[0].split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    return host


def normalize_url(value: str | None) -> str:
    if not value:
        return ""
    raw = value.strip().lower()
    if not raw:
        return ""
    parsed = urlparse(raw if "://" in raw else f"https://{raw}")
    host = parsed.netloc
    path = parsed.path.rstrip("/")
    if host.startswith("www."):
        host = host[4:]
    return f"{host}{path}"


def dump_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False)


def configure_logging() -> None:
    ensure_dirs()
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()

    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    app_handler = logging.FileHandler(LOG_DIR / "app.log", encoding="utf-8")
    app_handler.setFormatter(formatter)
    root.addHandler(app_handler)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    root.addHandler(console_handler)

    api_logger = logging.getLogger("api_calls")
    api_logger.setLevel(logging.INFO)
    api_logger.handlers.clear()
    api_handler = logging.FileHandler(LOG_DIR / "api_calls.log", encoding="utf-8")
    api_handler.setFormatter(formatter)
    api_logger.addHandler(api_handler)
    api_logger.propagate = False


def chunked(items: list[Any], size: int) -> list[list[Any]]:
    return [items[i : i + size] for i in range(0, len(items), size)]
