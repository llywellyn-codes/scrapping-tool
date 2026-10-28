"""Command-line tasks: `flask --app prospect_finder <command>`."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import click
from flask import Flask, current_app

from .export import export_prospects_csv, export_suppression_csv
from .importers import openstreetmap
from .importers.csv_import import import_csv
from .importers.google_places import GooglePlacesClient, GooglePlacesError
from .models import utcnow
from .output import export_prospects_json, prospect_record
from .repository import Repository, connect, init_db
from .scanner import make_fetcher, scan_business
from .scoring import TIER_LABELS
from .service import all_prospects, score_business


def _repo() -> Repository:
    cfg = current_app.config["PF"]
    conn = connect(cfg.database_path)
    init_db(conn)
    return Repository(conn, google_cache_days=cfg.google_places_cache_days)


def init_app(app: Flask) -> None:
    @app.cli.command("init-db")
    def init_db_cmd():
        """Create the database tables."""
        _repo()
        click.echo(f"Database ready at {current_app.config['PF'].database_path}")

    @app.cli.command("import-csv")
    @click.argument("path", type=click.Path(exists=True, dir_okay=False))
    @click.option("--source-name", required=True, help="Where the list came from.")
    @click.option("--source-url", default=None)
    @click.option("--terms-note", default="", help="What the provider's terms allow.")
    @click.option("--storage-permitted", is_flag=True, help="Confirm the terms allow storing this data.")
    @click.option("--export-permitted", is_flag=True, help="Confirm the terms allow exporting this data.")
    def import_csv_cmd(path, source_name, source_url, terms_note, storage_permitted, export_permitted):
        """Import prospects from a CSV file."""
        report = import_csv(_repo(), Path(path).read_text(encoding="utf-8-sig"), source_name=source_name,
                            source_url=source_url, terms_note=terms_note, storage_permitted=storage_permitted,
                            export_permitted=export_permitted)
        click.echo(f"batch {report.batch_id}: {report.counts}")
        for row in report.rows:
            if row.action in ("rejected", "suppressed"):
                click.echo(f"  row {row.row}: {row.action} - {row.reason}")

    @app.cli.command("osm-import")
    @click.argument("state")
    @click.option("--city", default=None)
    def osm_import_cmd(state, city):
        """Import roofers tagged in OpenStreetMap for a US state."""
        cfg = current_app.config["PF"]
        records = openstreetmap.search(state, city=city, overpass_url=cfg.overpass_url, user_agent=cfg.user_agent)
        click.echo(openstreetmap.import_osm(_repo(), records, f"{city + ', ' if city else ''}{state.upper()}"))

    @app.cli.command("scan")
    @click.option("--id", "ids", type=int, multiple=True, help="Prospect ID (repeatable).")
    @click.option("--all", "scan_all", is_flag=True, help="Scan every prospect with a website.")
    @click.option("--stale-only", is_flag=True, help="With --all: skip prospects checked in the last 30 days.")
    def scan_cmd(ids, scan_all, stale_only):
        """Scan public websites (sequential, rate-limited, robots.txt respected)."""
        cfg = current_app.config["PF"]
        repo = _repo()
        fetcher = make_fetcher(cfg)
        google = GooglePlacesClient(cfg.google_places_api_key) if cfg.google_places_api_key else None
        targets = [repo.get_business(i) for i in ids] if ids else (repo.list_businesses() if scan_all else [])
        if not targets:
            raise click.UsageError("Pass --id N or --all.")
        cutoff = utcnow() - timedelta(days=30)
        for b in targets:
            if b is None:
                continue
            if stale_only and b.last_checked_at and b.last_checked_at > cutoff:
                continue
            if repo.get_outreach(b.id).get("opted_out"):
                click.echo(f"#{b.id} skipped (opted out)")
                continue
            try:
                result = scan_business(repo, b, fetcher, cfg, google)
            except GooglePlacesError as exc:
                click.echo(f"#{b.id} {b.display_name}: skipped - {exc}")
                continue
            click.echo(f"#{b.id} {b.display_name}: {result.status} - {result.detail}")

    @app.cli.command("report")
    @click.option("--id", "business_id", type=int, default=None)
    @click.option("--json", "as_json", is_flag=True, help="Print JSON records (export rules applied).")
    def report_cmd(business_id, as_json):
        """Print score cards."""
        repo = _repo()
        prospects = [score_business(repo, repo.get_business(business_id))] if business_id else all_prospects(repo)
        if as_json:
            batches = repo.batches()
            records = [prospect_record(p, batches, repo.google_cache_days) for p in prospects]
            click.echo(json.dumps(records[0] if business_id else records, indent=2))
            return
        for p in sorted(prospects, key=lambda p: -p.card.total):
            c = p.card
            cats = " / ".join(f"{cs.score:g}" for cs in c.categories.values())
            click.echo(f"#{p.business.id} {p.business.display_name} [{p.business.location}]")
            click.echo(f"  score {c.total:g} (up to {c.upper_bound:g}) | fit/opp/ready {cats} | coverage "
                       f"{c.coverage:.0%} | confidence {c.confidence:.2f} | {TIER_LABELS[c.tier]}")
            for r in c.top_findings:
                click.echo(f"  + {r.signal.label} ({r.points:g}): {r.finding.observation} [{r.finding.source_url}]")
            for reason in c.review_reasons:
                click.echo(f"  ! {reason}")
            click.echo(f"  next: {c.next_step}")

    @app.cli.command("export")
    @click.argument("path", type=click.Path(dir_okay=False))
    @click.option("--suppression", is_flag=True, help="Export the opt-out suppression list instead.")
    def export_cmd(path, suppression):
        """Write exportable prospect fields to CSV, or JSON when PATH ends in .json (or the suppression list)."""
        repo = _repo()
        if suppression:
            text = export_suppression_csv(repo)
        elif path.lower().endswith(".json"):
            text = export_prospects_json(repo, all_prospects(repo))
        else:
            text = export_prospects_csv(repo, all_prospects(repo))
        Path(path).write_text(text, encoding="utf-8")
        click.echo(f"wrote {path}")

    @app.cli.command("purge-expired")
    def purge_cmd():
        """Delete source content whose retention period has ended."""
        click.echo(_repo().purge_expired())
