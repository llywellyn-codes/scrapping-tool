"""Import prospects from a CSV file the user has the right to use.

Typical permitted inputs: state contractor-license exports published as public
records, your own research spreadsheets, or purchased lists whose licence allows
storage. The importer records where each field came from and whether the batch
may be exported.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field

from ..repository import Provenance, Repository
from ..sources import CSV_IMPORT

US_STATES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA", "HI", "ID", "IL", "IN", "IA", "KS", "KY",
    "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH",
    "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY", "PR",
}

COLUMN_ALIASES = {
    "name": ("name", "business_name", "business", "company", "company_name", "dba"),
    "phone": ("phone", "telephone", "phone_number", "tel"),
    "email": ("email", "email_address"),
    "website": ("website", "url", "web", "site", "domain"),
    "street": ("street", "address", "address1", "street_address"),
    "city": ("city", "town"),
    "state": ("state", "st", "state_code"),
    "postal_code": ("postal_code", "zip", "zipcode", "zip_code"),
    "source_url": ("source_url", "record_url", "evidence_url"),
}


@dataclass
class RowResult:
    row: int
    action: str
    reason: str = ""
    business_id: int | None = None


@dataclass
class ImportReport:
    batch_id: int
    counts: dict[str, int] = field(default_factory=lambda: {"created": 0, "merged": 0, "suppressed": 0,
                                                             "rejected": 0})
    rows: list[RowResult] = field(default_factory=list)


def _map_headers(headers: list[str]) -> dict[str, str]:
    normalized = {h: h.strip().lower().replace(" ", "_").replace("-", "_") for h in headers}
    mapping = {}
    for target, aliases in COLUMN_ALIASES.items():
        for original, norm in normalized.items():
            if norm in aliases and target not in mapping:
                mapping[target] = original
    return mapping


def import_csv(repo: Repository, text: str, *, source_name: str, storage_permitted: bool,
               export_permitted: bool = False, terms_note: str = "", source_url: str | None = None) -> ImportReport:
    if not storage_permitted:
        raise PermissionError("Confirm that the data provider's terms allow you to store this data.")
    if not source_name.strip():
        raise ValueError("Name the data source (e.g. 'Florida DBPR licensee file').")
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    if not reader.fieldnames:
        raise ValueError("The CSV file has no header row.")
    mapping = _map_headers(reader.fieldnames)
    if not ({"name", "phone", "website"} & mapping.keys()):
        raise ValueError("The CSV needs at least one of these columns: name, phone, website.")

    batch_id = repo.create_batch(CSV_IMPORT, source_name.strip(), source_url=source_url, terms_note=terms_note,
                                 export_permitted=export_permitted)
    report = ImportReport(batch_id)
    for i, row in enumerate(reader, start=2):  # row 1 is the header
        values = {k: (row.get(col) or "").strip() for k, col in mapping.items()}
        state = values.get("state", "").upper()
        if state and state not in US_STATES:
            result = RowResult(i, "rejected", f"'{state}' is not a US state code")
        else:
            row_source = values.pop("source_url", "") or source_url
            prov = Provenance(CSV_IMPORT, url=row_source or None, batch_id=batch_id)
            outcome = repo.upsert(values, prov)
            result = RowResult(i, outcome.action, outcome.reason, outcome.business_id)
        report.counts[result.action] += 1
        report.rows.append(result)
    repo.finish_batch(batch_id, report.counts)
    return report
