"""SQLite storage for prospects, findings, outreach and suppression."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from importlib import resources

from . import dedupe
from .models import Business, Finding, Outcome, format_ts, parse_ts, utcnow
from .scoring import evidence_problems
from .sources import MANUAL, expiry_for, may_store

IDENTITY_FIELDS = ("name", "phone", "email", "website", "street", "city", "state", "postal_code")
OUTREACH_STATUSES = (
    "new", "researching", "ready", "contacted", "replied", "meeting_booked", "client", "not_interested",
)


class EvidenceError(ValueError):
    """A finding was rejected because its evidence is incomplete."""


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, detect_types=0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    schema = resources.files("prospect_finder").joinpath("schema.sql").read_text()
    conn.executescript(schema)
    conn.commit()


@dataclass
class Provenance:
    source: str
    url: str | None = None
    at: datetime | None = None
    batch_id: int | None = None

    def as_dict(self, google_cache_days: int) -> dict:
        at = self.at or utcnow()
        expires = expiry_for(self.source, at, google_cache_days)
        return {
            "source": self.source,
            "url": self.url,
            "at": format_ts(at),
            "batch_id": self.batch_id,
            "expires_at": format_ts(expires),
        }


@dataclass
class UpsertResult:
    business_id: int | None
    action: str  # created | merged | suppressed | rejected
    reason: str = ""


class Repository:
    def __init__(self, conn: sqlite3.Connection, google_cache_days: int = 0):
        self.conn = conn
        self.google_cache_days = google_cache_days

    # ------------------------------------------------------------ businesses

    def _row_to_business(self, row: sqlite3.Row) -> Business:
        return Business(
            id=row["id"],
            name=row["name"],
            phone=row["phone"],
            email=row["email"],
            website=row["website"],
            street=row["street"],
            city=row["city"],
            state=row["state"],
            postal_code=row["postal_code"],
            google_place_id=row["google_place_id"],
            osm_id=row["osm_id"],
            primary_source=row["primary_source"],
            field_sources=json.loads(row["field_sources"] or "{}"),
            created_at=parse_ts(row["created_at"]),
            last_checked_at=parse_ts(row["last_checked_at"]),
        )

    def get_business(self, business_id: int) -> Business | None:
        row = self.conn.execute("SELECT * FROM businesses WHERE id = ?", (business_id,)).fetchone()
        return self._row_to_business(row) if row else None

    def list_businesses(self) -> list[Business]:
        rows = self.conn.execute("SELECT * FROM businesses ORDER BY id").fetchall()
        return [self._row_to_business(r) for r in rows]

    def _save_business(self, b: Business) -> int:
        keys = dedupe.keys_for(b)
        values = dict(
            name=b.name, phone=b.phone, email=b.email, website=b.website, street=b.street, city=b.city,
            state=b.state, postal_code=b.postal_code, google_place_id=b.google_place_id, osm_id=b.osm_id,
            primary_source=b.primary_source, field_sources=json.dumps(b.field_sources, sort_keys=True),
            last_checked_at=format_ts(b.last_checked_at), **keys,
        )
        if b.id is None:
            values["created_at"] = format_ts(b.created_at or utcnow())
            cols = ", ".join(values)
            marks = ", ".join("?" for _ in values)
            cur = self.conn.execute(f"INSERT INTO businesses ({cols}) VALUES ({marks})", list(values.values()))
            b.id = cur.lastrowid
            self.conn.execute(
                "INSERT INTO outreach (business_id, status, updated_at) VALUES (?, 'new', ?)",
                (b.id, format_ts(utcnow())),
            )
        else:
            sets = ", ".join(f"{k} = ?" for k in values)
            self.conn.execute(f"UPDATE businesses SET {sets} WHERE id = ?", [*values.values(), b.id])
        self.conn.commit()
        return b.id

    def set_fields(self, b: Business, fields: dict[str, str | None], prov: Provenance,
                   overwrite: bool = False) -> list[str]:
        """Set identity fields with provenance; returns the names of fields changed.

        Content from a source that may not be stored is dropped. Existing values
        are kept unless ``overwrite`` is set or the existing value came from a
        source with a retention limit (so a permanent source can replace it).
        """
        if not may_store(prov.source, self.google_cache_days):
            return []
        changed = []
        for name, value in fields.items():
            if name not in IDENTITY_FIELDS:
                raise ValueError(f"unknown identity field {name}")
            value = (value or "").strip() or None
            if value is None:
                continue
            if name == "phone":
                value = dedupe.format_phone(value)
            if name == "website":
                value = dedupe.normalize_url(value)
                if value is None:
                    continue
            if name == "state":
                value = value.upper()
            current = getattr(b, name)
            current_prov = b.field_sources.get(name, {})
            replaceable = current is None or overwrite or bool(current_prov.get("expires_at"))
            if not replaceable or current == value and current_prov.get("source") == prov.source:
                continue
            setattr(b, name, value)
            b.field_sources[name] = prov.as_dict(self.google_cache_days)
            changed.append(name)
        return changed

    def create_business(self, fields: dict, prov: Provenance, *, google_place_id: str | None = None,
                        osm_id: str | None = None) -> Business:
        b = Business(google_place_id=google_place_id, osm_id=osm_id, primary_source=prov.source)
        self.set_fields(b, fields, prov)
        self._save_business(b)
        return b

    def save(self, b: Business) -> None:
        self._save_business(b)

    def find_match(self, candidate: Business) -> tuple[Business, str] | None:
        """Find an existing record for the same business (indexed lookups, then name check)."""
        keys = dedupe.keys_for(candidate)
        clauses, params = [], []
        for col, val in (("google_place_id", candidate.google_place_id), ("osm_id", candidate.osm_id),
                         ("domain_key", keys["domain_key"]), ("phone_key", keys["phone_key"]),
                         ("name_key", keys["name_key"])):
            if val:
                clauses.append(f"{col} = ?")
                params.append(val)
        if not clauses:
            return None
        rows = self.conn.execute(f"SELECT * FROM businesses WHERE {' OR '.join(clauses)} ORDER BY id",
                                 params).fetchall()
        for row in rows:
            existing = self._row_to_business(row)
            reason = dedupe.match_reason(candidate, existing)
            if reason:
                return existing, reason
        return None

    def is_suppressed(self, candidate: Business) -> str | None:
        keys = dedupe.keys_for(candidate)
        checks = [("phone", keys["phone_key"]), ("domain", keys["domain_key"]),
                  ("email", (candidate.email or "").lower() or None),
                  ("google_place_id", candidate.google_place_id), ("osm_id", candidate.osm_id)]
        for kind, value in checks:
            if value and self.conn.execute(
                "SELECT 1 FROM suppression WHERE kind = ? AND value_key = ?", (kind, value)
            ).fetchone():
                return f"{kind} is on the suppression list"
        return None

    def upsert(self, fields: dict, prov: Provenance, *, google_place_id: str | None = None,
               osm_id: str | None = None) -> UpsertResult:
        """Import one business: skip opted-out, merge into a duplicate, or create."""
        candidate = Business(google_place_id=google_place_id, osm_id=osm_id, primary_source=prov.source)
        # Build the candidate without the storage filter so matching can use all fields.
        for name, value in fields.items():
            if name in IDENTITY_FIELDS and value:
                setattr(candidate, name, str(value).strip())
        if not (candidate.name or candidate.phone or candidate.website or google_place_id or osm_id):
            return UpsertResult(None, "rejected", "row has no name, phone, website or source ID")
        suppressed = self.is_suppressed(candidate)
        if suppressed:
            return UpsertResult(None, "suppressed", suppressed)
        match = self.find_match(candidate)
        if match:
            existing, reason = match
            if self.get_outreach(existing.id)["opted_out"]:
                return UpsertResult(existing.id, "suppressed", "matching prospect has opted out")
            self.set_fields(existing, fields, prov)
            existing.google_place_id = existing.google_place_id or google_place_id
            existing.osm_id = existing.osm_id or osm_id
            self._save_business(existing)
            return UpsertResult(existing.id, "merged", reason)
        b = self.create_business(fields, prov, google_place_id=google_place_id, osm_id=osm_id)
        return UpsertResult(b.id, "created")

    def possible_duplicates(self) -> list[tuple[Business, Business, str]]:
        businesses = self.list_businesses()
        pairs = []
        for i, a in enumerate(businesses):
            for b in businesses[i + 1:]:
                reason = dedupe.match_reason(a, b) or dedupe.possible_duplicate_reason(a, b)
                if reason:
                    pairs.append((a, b, reason))
        return pairs

    def merge(self, keep_id: int, drop_id: int) -> None:
        """Merge ``drop_id`` into ``keep_id``: fill gaps, move findings, combine outreach."""
        keep, drop = self.get_business(keep_id), self.get_business(drop_id)
        if not keep or not drop or keep_id == drop_id:
            raise ValueError("both prospects must exist and differ")
        for name in IDENTITY_FIELDS:
            if getattr(keep, name) is None and getattr(drop, name) is not None:
                setattr(keep, name, getattr(drop, name))
                if name in drop.field_sources:
                    keep.field_sources[name] = drop.field_sources[name]
        keep.google_place_id = keep.google_place_id or drop.google_place_id
        keep.osm_id = keep.osm_id or drop.osm_id
        a, b = self.get_outreach(keep_id), self.get_outreach(drop_id)
        merged_outreach = dict(a)
        merged_outreach["opted_out"] = a["opted_out"] or b["opted_out"]
        merged_outreach["opted_out_at"] = a["opted_out_at"] or b["opted_out_at"]
        merged_outreach["last_contact_date"] = max(filter(None, [a["last_contact_date"], b["last_contact_date"]]),
                                                   default=None)
        # Keep whichever record has real outreach history; if both do, the most recently updated.
        if b["status"] != "new" and (a["status"] == "new" or (b["updated_at"] or "") > (a["updated_at"] or "")):
            merged_outreach["status"] = b["status"]
        merged_outreach["response"] = "\n".join(filter(None, [a["response"], b["response"]])) or None
        merged_outreach["notes"] = "\n".join(filter(None, [a["notes"], b["notes"]])) or None
        self.conn.execute("UPDATE findings SET business_id = ? WHERE business_id = ?", (keep_id, drop_id))
        self.conn.execute("UPDATE scan_log SET business_id = ? WHERE business_id = ?", (keep_id, drop_id))
        # Drop the old record before saving so unique source IDs can move across.
        self.conn.execute("DELETE FROM businesses WHERE id = ?", (drop_id,))
        self._save_business(keep)
        self._write_outreach(keep_id, merged_outreach)
        if merged_outreach["opted_out"]:
            self.opt_out(keep_id, reason="merged record had opted out")

    # ------------------------------------------------------------ findings

    def add_finding(self, business_id: int, f: Finding, created_by: str = "system") -> int | None:
        """Store a finding after validating its evidence.

        Findings from a source that may not be stored are dropped (returns None).
        Determinate findings with incomplete evidence raise ``EvidenceError``.
        """
        if not may_store(f.source_type, self.google_cache_days):
            return None
        problems = evidence_problems(f)
        if problems:
            raise EvidenceError("; ".join(problems))
        if f.expires_at is None and f.observed_at is not None:
            f.expires_at = expiry_for(f.source_type, f.observed_at, self.google_cache_days)
        cur = self.conn.execute(
            """INSERT INTO findings (business_id, signal_key, outcome, evidence_type, confidence, source_type,
                   source_url, observation, observed_at, test_name, test_result, expires_at, created_by, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (business_id, f.signal_key, f.outcome.value, f.evidence_type.value, f.confidence, f.source_type,
             f.source_url, f.observation, format_ts(f.observed_at), f.test_name, f.test_result,
             format_ts(f.expires_at), created_by, format_ts(utcnow())),
        )
        self.conn.commit()
        return cur.lastrowid

    def replace_source_findings(self, business_id: int, source_type: str, findings: list[Finding]) -> None:
        """Record a new automated check, replacing that source's earlier results per signal.

        A determinate new result replaces every earlier result from the same
        source for that signal. An unknown result only replaces earlier unknowns,
        so a blocked re-scan does not erase older evidence (which will go stale).
        """
        for f in findings:
            if f.outcome == Outcome.UNKNOWN:
                self.conn.execute(
                    "DELETE FROM findings WHERE business_id = ? AND source_type = ? AND signal_key = ? "
                    "AND outcome = 'unknown'", (business_id, source_type, f.signal_key))
            else:
                self.conn.execute(
                    "DELETE FROM findings WHERE business_id = ? AND source_type = ? AND signal_key = ?",
                    (business_id, source_type, f.signal_key))
        for f in findings:
            self.add_finding(business_id, f, created_by=source_type)
        self.conn.commit()

    def _row_to_finding(self, row: sqlite3.Row) -> Finding:
        return Finding(
            id=row["id"], business_id=row["business_id"], signal_key=row["signal_key"],
            outcome=row["outcome"], evidence_type=row["evidence_type"], confidence=row["confidence"],
            source_type=row["source_type"], source_url=row["source_url"], observation=row["observation"],
            observed_at=row["observed_at"], test_name=row["test_name"], test_result=row["test_result"],
            expires_at=row["expires_at"],
        )

    def findings_for(self, business_id: int) -> list[Finding]:
        rows = self.conn.execute("SELECT * FROM findings WHERE business_id = ? ORDER BY id",
                                 (business_id,)).fetchall()
        return [self._row_to_finding(r) for r in rows]

    def all_findings(self) -> dict[int, list[Finding]]:
        out: dict[int, list[Finding]] = {}
        for row in self.conn.execute("SELECT * FROM findings ORDER BY id"):
            out.setdefault(row["business_id"], []).append(self._row_to_finding(row))
        return out

    def delete_finding(self, finding_id: int) -> None:
        self.conn.execute("DELETE FROM findings WHERE id = ?", (finding_id,))
        self.conn.commit()

    # ------------------------------------------------------------ outreach

    def get_outreach(self, business_id: int) -> dict:
        row = self.conn.execute("SELECT * FROM outreach WHERE business_id = ?", (business_id,)).fetchone()
        return dict(row) if row else {}

    def all_outreach(self) -> dict[int, dict]:
        return {r["business_id"]: dict(r) for r in self.conn.execute("SELECT * FROM outreach")}

    def update_outreach(self, business_id: int, *, status: str | None = None, last_contact_date: str | None = None,
                        response: str | None = None, notes: str | None = None) -> None:
        current = self.get_outreach(business_id)
        if status is not None and status not in OUTREACH_STATUSES:
            raise ValueError(f"unknown outreach status {status}")
        if current.get("opted_out") and status in ("contacted", "ready"):
            raise ValueError("this prospect has opted out and cannot be contacted")
        if last_contact_date:
            datetime.strptime(last_contact_date, "%Y-%m-%d")  # validate
        self._write_outreach(business_id, {
            "status": status or current["status"],
            "last_contact_date": (last_contact_date or None) if last_contact_date is not None
            else current["last_contact_date"],
            "response": (response or None) if response is not None else current["response"],
            "notes": (notes or None) if notes is not None else current["notes"],
        })

    def _write_outreach(self, business_id: int, values: dict) -> None:
        self.conn.execute(
            "UPDATE outreach SET status = ?, last_contact_date = ?, response = ?, notes = ?, updated_at = ? "
            "WHERE business_id = ?",
            (values["status"], values["last_contact_date"], values["response"], values["notes"],
             format_ts(utcnow()), business_id),
        )
        self.conn.commit()

    def opt_out(self, business_id: int, reason: str = "requested no contact") -> None:
        b = self.get_business(business_id)
        now = format_ts(utcnow())
        self.conn.execute(
            "UPDATE outreach SET opted_out = 1, opted_out_at = COALESCE(opted_out_at, ?), updated_at = ? "
            "WHERE business_id = ?", (now, now, business_id))
        keys = dedupe.keys_for(b)
        for kind, value in (("phone", keys["phone_key"]), ("domain", keys["domain_key"]),
                            ("email", (b.email or "").lower() or None),
                            ("google_place_id", b.google_place_id), ("osm_id", b.osm_id)):
            if value:
                self.conn.execute(
                    "INSERT OR IGNORE INTO suppression (kind, value_key, reason, created_at) VALUES (?, ?, ?, ?)",
                    (kind, value, reason, now))
        self.conn.commit()

    def suppression_list(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute("SELECT kind, value_key, reason, created_at FROM suppression "
                                                   "ORDER BY created_at")]

    # ------------------------------------------------------------ imports, scans, retention

    def create_batch(self, source_type: str, source_name: str, *, source_url: str | None = None,
                     terms_note: str | None = None, export_permitted: bool = False) -> int:
        cur = self.conn.execute(
            "INSERT INTO import_batches (source_type, source_name, source_url, terms_note, export_permitted, "
            "imported_at) VALUES (?, ?, ?, ?, ?, ?)",
            (source_type, source_name, source_url, terms_note, int(export_permitted), format_ts(utcnow())))
        self.conn.commit()
        return cur.lastrowid

    def finish_batch(self, batch_id: int, counts: dict[str, int]) -> None:
        self.conn.execute(
            "UPDATE import_batches SET rows_total = ?, rows_created = ?, rows_merged = ?, rows_suppressed = ?, "
            "rows_rejected = ? WHERE id = ?",
            (sum(counts.values()), counts.get("created", 0), counts.get("merged", 0), counts.get("suppressed", 0),
             counts.get("rejected", 0), batch_id))
        self.conn.commit()

    def batches(self) -> dict[int, dict]:
        return {r["id"]: dict(r) for r in self.conn.execute("SELECT * FROM import_batches ORDER BY id DESC")}

    def log_scan(self, business_id: int, started_at: datetime, status: str, detail: str) -> None:
        self.conn.execute(
            "INSERT INTO scan_log (business_id, started_at, finished_at, status, detail) VALUES (?, ?, ?, ?, ?)",
            (business_id, format_ts(started_at), format_ts(utcnow()), status, detail))
        self.conn.commit()

    def scan_history(self, business_id: int) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM scan_log WHERE business_id = ? ORDER BY id DESC LIMIT 10", (business_id,))]

    def touch_checked(self, b: Business, when: datetime | None = None) -> None:
        b.last_checked_at = when or utcnow()
        self._save_business(b)

    def purge_expired(self, now: datetime | None = None) -> dict[str, int]:
        """Delete content whose source retention period has ended (place IDs are kept)."""
        now = now or utcnow()
        cur = self.conn.execute("DELETE FROM findings WHERE expires_at IS NOT NULL AND expires_at <= ?",
                                (format_ts(now),))
        purged_findings = cur.rowcount
        purged_fields = 0
        for b in self.list_businesses():
            changed = False
            for name, prov in list(b.field_sources.items()):
                expires = parse_ts(prov.get("expires_at"))
                if expires and expires <= now:
                    setattr(b, name, None)
                    del b.field_sources[name]
                    purged_fields += 1
                    changed = True
            if changed:
                self._save_business(b)
        self.conn.commit()
        return {"findings": purged_findings, "fields": purged_fields}


def manual_provenance(url: str | None = None) -> Provenance:
    return Provenance(MANUAL, url=url)

