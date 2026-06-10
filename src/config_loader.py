from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd
import yaml

from .models import CampaignConfig


def load_campaign_config(path: str | Path) -> CampaignConfig:
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    suffix = config_path.suffix.lower()
    if suffix in {".yaml", ".yml"}:
        data = _load_yaml(config_path)
    elif suffix in {".xlsx", ".xls"}:
        data = _load_excel(config_path)
    else:
        raise ValueError("Config must be a YAML or Excel file")
    return CampaignConfig.model_validate(data)


def _load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ValueError("YAML config must contain a mapping/object")
    return data


def _load_excel(path: Path) -> dict[str, Any]:
    workbook = pd.read_excel(path, sheet_name=None)
    if "campaign" not in workbook:
        raise ValueError("Excel config must include a 'campaign' sheet")
    campaign_rows = workbook["campaign"].to_dict("records")
    if not campaign_rows:
        raise ValueError("Excel 'campaign' sheet is empty")
    base = {k: v for k, v in campaign_rows[0].items() if pd.notna(v)}

    countries = _sheet_values(workbook, "countries", "country")
    keywords = _sheet_values(workbook, "keywords", "keyword")
    titles = {
        "tier_1": _sheet_values(workbook, "titles_tier_1", "title"),
        "tier_2": _sheet_values(workbook, "titles_tier_2", "title"),
        "tier_3": _sheet_values(workbook, "titles_tier_3", "title"),
    }

    limits = {}
    if "limits" in workbook:
        for row in workbook["limits"].to_dict("records"):
            key = row.get("key")
            value = row.get("value")
            if pd.notna(key) and pd.notna(value):
                limits[str(key)] = int(value)

    return {
        "campaign_name": base.get("campaign_name", ""),
        "countries": countries,
        "keywords": keywords,
        "limits": limits,
        "titles": titles,
    }


def _sheet_values(workbook: dict[str, pd.DataFrame], sheet: str, column: str) -> list[str]:
    if sheet not in workbook:
        return []
    if column not in workbook[sheet].columns:
        raise ValueError(f"Excel sheet '{sheet}' must include column '{column}'")
    return [str(v).strip() for v in workbook[sheet][column].tolist() if pd.notna(v) and str(v).strip()]
