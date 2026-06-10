from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values

from .config_loader import load_campaign_config
from .utils import campaign_output_dir


@dataclass
class DoctorResult:
    ok: bool
    lines: list[str]


def run_doctor(config_path: str, env_path: str | Path = ".env") -> DoctorResult:
    lines: list[str] = []
    ok = True

    try:
        config = load_campaign_config(config_path)
        lines.append(f"OK config: {config.campaign_name}")
        lines.append(f"OK planned countries: {len(config.countries)}")
        lines.append(f"OK planned keywords: {len(config.keywords)}")
        lines.append(f"OK campaign output folder: {campaign_output_dir(config.campaign_name)}")
    except Exception as exc:
        lines.append(f"ERROR config: {exc}")
        return DoctorResult(False, lines)

    env_file = Path(env_path)
    if not env_file.exists():
        lines.append("ERROR .env: file is missing. Copy .env.example to .env and add APOLLO_API_KEY.")
        return DoctorResult(False, lines)

    values = dotenv_values(env_file)
    api_key = (values.get("APOLLO_API_KEY") or "").strip()
    if not api_key:
        ok = False
        lines.append("ERROR APOLLO_API_KEY: missing")
    elif api_key.lower() in {"your_apollo_api_key_here", "your_api_key_here", "apollo_api_key", "..."}:
        ok = False
        lines.append("ERROR APOLLO_API_KEY: still the example placeholder, not your real key")
    else:
        lines.append(f"OK APOLLO_API_KEY: present, length={len(api_key)}")

    base_url = (values.get("APOLLO_BASE_URL") or "").strip()
    if not base_url:
        ok = False
        lines.append("ERROR APOLLO_BASE_URL: missing")
    else:
        lines.append(f"OK APOLLO_BASE_URL: {base_url}")

    webhook_url = (values.get("APOLLO_WEBHOOK_URL") or "").strip()
    if webhook_url:
        lines.append("OK APOLLO_WEBHOOK_URL: configured")
    else:
        lines.append("INFO APOLLO_WEBHOOK_URL: empty, waterfall callbacks will not be requested")

    lines.append("No Apollo API calls were made.")
    return DoctorResult(ok, lines)
