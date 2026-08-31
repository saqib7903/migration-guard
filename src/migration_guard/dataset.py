"""Load the labelled migration cases from eval/dataset/cases/."""
from __future__ import annotations

import json
from pathlib import Path

from .config import REPO_ROOT
from .models import MigrationCase

DATASET_DIR = REPO_ROOT / "eval" / "dataset" / "cases"


def load_cases(only: list[str] | None = None, directory: Path | None = None) -> list[MigrationCase]:
    directory = directory or DATASET_DIR
    cases: list[MigrationCase] = []
    for d in sorted(p for p in directory.iterdir() if p.is_dir()):
        if only and d.name not in only:
            continue
        label = json.loads((d / "label.json").read_text(encoding="utf-8"))
        cases.append(
            MigrationCase(
                case_id=d.name,
                title=label.get("title", d.name),
                base_schema_sql=(d / "schema.sql").read_text(encoding="utf-8"),
                migration_sql=(d / "migration.sql").read_text(encoding="utf-8"),
                expected_risks=label.get("expected_risks", []),
                expected_decision=label.get("expected_decision", ""),
                notes=label.get("notes", ""),
            )
        )
    return cases


def load_case_from_files(schema_path: Path, migration_path: Path) -> MigrationCase:
    return MigrationCase(
        case_id=migration_path.stem,
        title=migration_path.name,
        base_schema_sql=schema_path.read_text(encoding="utf-8"),
        migration_sql=migration_path.read_text(encoding="utf-8"),
    )
