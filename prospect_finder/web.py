"""Flask routes for the local web interface."""

from __future__ import annotations

import json
import re
import secrets
from datetime import datetime

from flask import (
    Blueprint, Flask, Response, abort, current_app, flash, g, redirect, render_template, request, session, url_for,
)

from .audit import AuditRefused, build_audit, polish_with_claude
from .export import export_prospects_csv, export_suppression_csv
from .importers import google_places, openstreetmap
from .importers.csv_import import import_csv
from .models import EvidenceType, Finding, Outcome, utcnow
from .repository import IDENTITY_FIELDS, OUTREACH_STATUSES, EvidenceError, Provenance, Repository, connect, init_db
from .rubric import CATEGORY_LABELS, CATEGORY_MAX, DEFAULT_POLICY, GROUPS, SIGNALS, SIGNALS_BY_KEY
from .scanner import make_fetcher, scan_business
from .scoring import TIER_LABELS, SignalState
from .service import all_prospects, score_business
from .sources import MANUAL, policies

bp = Blueprint("pf", __name__)


def cfg():
    return current_app.config["PF"]


def get_repo() -> Repository:
    if "repo" not in g:
        conn = connect(cfg().database_path)
        g.repo = Repository(conn, google_cache_days=cfg().google_places_cache_days)
    return g.repo


def init_app(app: Flask) -> None:
    with app.app_context():
        conn = connect(app.config["PF"].database_path)
        init_db(conn)
        Repository(conn, app.config["PF"].google_places_cache_days).purge_expired()
        conn.close()

    @app.teardown_appcontext
    def close_db(_exc):
        repo = g.pop("repo", None)
        if repo is not None:
            repo.conn.close()

    @app.before_request
    def csrf_protect():
        if request.method == "POST":
            token = session.get("csrf_token")
            if not token or token != request.form.get("csrf_token"):
                abort(400, "Missing or invalid form token; reload the page and try again.")

    @app.context_processor
    def inject():
        if "csrf_token" not in session:
            session["csrf_token"] = secrets.token_hex(16)
        return {
            "csrf_token": session["csrf_token"],
            "TIER_LABELS": TIER_LABELS,
            "CATEGORY_LABELS": CATEGORY_LABELS,
            "CATEGORY_MAX": CATEGORY_MAX,
            "google_enabled": bool(cfg().google_places_api_key),
            "llm_enabled": bool(cfg().anthropic_api_key),
        }

    @app.template_filter("pct")
    def pct(value):
        return f"{value:.0%}"

    @app.template_filter("num")
    def num(value):
        return f"{value:g}"

    @app.template_filter("dt")
    def dt(value):
        if not value:
            return "—"
        if isinstance(value, str):
            return value[:16].replace("T", " ")
        return value.strftime("%Y-%m-%d %H:%M")

    app.register_blueprint(bp)


# ---------------------------------------------------------------- prospects


@bp.get("/")
def index():
    repo = get_repo()
    prospects = all_prospects(repo)
    tier = request.args.get("tier", "")
    status = request.args.get("status", "")
    q = request.args.get("q", "").strip().lower()
    if tier:
        prospects = [p for p in prospects if p.card.tier == tier]
    if status:
        prospects = [p for p in prospects if p.outreach.get("status") == status]
    if q:
        prospects = [p for p in prospects if q in (p.business.display_name + " " + p.business.location).lower()]
    sort = request.args.get("sort", "score")
    keys = {
        "score": lambda p: (-p.card.total, -p.card.coverage),
        "coverage": lambda p: (-p.card.coverage, -p.card.total),
        "name": lambda p: p.business.display_name.lower(),
    }
    prospects.sort(key=keys.get(sort, keys["score"]))
    return render_template("index.html", prospects=prospects, statuses=OUTREACH_STATUSES,
                           filters={"tier": tier, "status": status, "q": q, "sort": sort})


@bp.post("/prospects/new")
def new_prospect():
    repo = get_repo()
    fields = {f: request.form.get(f, "") for f in IDENTITY_FIELDS}
    if not (fields["name"] or fields["website"] or fields["phone"]):
        flash("Enter at least a name, website or phone.", "error")
        return redirect(url_for("pf.import_page"))
    result = repo.upsert(fields, Provenance(MANUAL, url=request.form.get("source_url") or None))
    if result.action == "merged":
        flash(f"Matched an existing prospect ({result.reason}); details merged.", "info")
    elif result.action == "suppressed":
        flash(f"Not added: {result.reason}.", "error")
        return redirect(url_for("pf.import_page"))
    return redirect(url_for("pf.prospect", business_id=result.business_id))


def _load(business_id: int):
    repo = get_repo()
    b = repo.get_business(business_id)
    if not b:
        abort(404)
    return repo, b


@bp.get("/prospects/<int:business_id>")
def prospect(business_id: int):
    repo, b = _load(business_id)
    p = score_business(repo, b)
    return render_template(
        "prospect.html", p=p, b=b, card=p.card, SignalState=SignalState, statuses=OUTREACH_STATUSES,
        signals=SIGNALS, outcomes=list(Outcome), evidence_types=list(EvidenceType),
        identity_fields=IDENTITY_FIELDS, scans=repo.scan_history(b.id), sources=policies(cfg().google_places_cache_days),
        batches=repo.batches(),
    )


@bp.post("/prospects/<int:business_id>/identity")
def edit_identity(business_id: int):
    repo, b = _load(business_id)
    url = request.form.get("source_url", "").strip() or None
    fields = {f: request.form.get(f, "") for f in IDENTITY_FIELDS if request.form.get(f, "") != (getattr(b, f) or "")}
    changed = repo.set_fields(b, fields, Provenance(MANUAL, url=url), overwrite=True)
    repo.save(b)
    flash(f"Updated: {', '.join(changed)}" if changed else "No changes.", "info")
    return redirect(url_for("pf.prospect", business_id=business_id))


@bp.post("/prospects/<int:business_id>/outreach")
def update_outreach(business_id: int):
    repo, _ = _load(business_id)
    try:
        repo.update_outreach(
            business_id,
            status=request.form.get("status") or None,
            last_contact_date=request.form.get("last_contact_date", ""),
            response=request.form.get("response", ""),
            notes=request.form.get("notes", ""),
        )
        flash("Outreach updated.", "info")
    except ValueError as exc:
        flash(str(exc), "error")
    return redirect(url_for("pf.prospect", business_id=business_id))


@bp.post("/prospects/<int:business_id>/opt-out")
def opt_out(business_id: int):
    repo, _ = _load(business_id)
    repo.opt_out(business_id, reason=request.form.get("reason") or "requested no contact")
    flash("Marked as opted out. Phone, website, email and source IDs were added to the suppression list.", "info")
    return redirect(url_for("pf.prospect", business_id=business_id))


@bp.post("/prospects/<int:business_id>/findings")
def add_finding(business_id: int):
    repo, _ = _load(business_id)
    form = request.form
    key = form.get("signal_key", "")
    if key not in SIGNALS_BY_KEY:
        abort(400, "unknown signal")
    outcome = Outcome(form.get("outcome", "unknown"))
    observed = form.get("observed_on", "").strip()
    try:
        observed_at = datetime.fromisoformat(observed).replace(tzinfo=utcnow().tzinfo) if observed else utcnow()
        f = Finding(
            signal_key=key,
            outcome=outcome,
            evidence_type=EvidenceType.UNKNOWN if outcome == Outcome.UNKNOWN else EvidenceType(form.get("evidence_type")),
            confidence=float(form.get("confidence") or 0),
            source_type=MANUAL,
            observed_at=observed_at,
            source_url=form.get("source_url", "").strip() or None,
            observation=form.get("observation", "").strip() or None,
            test_name=form.get("test_name", "").strip() or None,
            test_result=form.get("test_result", "").strip() or None,
        )
        repo.add_finding(business_id, f, created_by="manual")
        repo.touch_checked(repo.get_business(business_id))
        flash(f"Finding recorded for '{SIGNALS_BY_KEY[key].label}'.", "info")
    except (EvidenceError, ValueError) as exc:
        flash(f"Finding not saved: {exc}", "error")
    return redirect(url_for("pf.prospect", business_id=business_id) + "#signals")


@bp.post("/prospects/<int:business_id>/findings/<int:finding_id>/delete")
def delete_finding(business_id: int, finding_id: int):
    repo, _ = _load(business_id)
    if any(f.id == finding_id for f in repo.findings_for(business_id)):
        repo.delete_finding(finding_id)
        flash("Finding deleted.", "info")
    return redirect(url_for("pf.prospect", business_id=business_id) + "#signals")


@bp.post("/prospects/<int:business_id>/delete")
def delete_prospect(business_id: int):
    repo, b = _load(business_id)
    repo.conn.execute("DELETE FROM businesses WHERE id = ?", (business_id,))
    repo.conn.commit()
    flash(f"Deleted {b.display_name}. Opt-outs stay on the suppression list.", "info")
    return redirect(url_for("pf.index"))


# ---------------------------------------------------------------- import, duplicates, export, rubric


@bp.route("/import", methods=["GET", "POST"])
def import_page():
    repo = get_repo()
    report = None
    if request.method == "POST":
        upload = request.files.get("file")
        if not upload or not upload.filename:
            flash("Choose a CSV file.", "error")
        else:
            try:
                text = upload.read().decode("utf-8-sig", errors="replace")
                report = import_csv(
                    repo, text,
                    source_name=request.form.get("source_name", ""),
                    source_url=request.form.get("source_url", "").strip() or None,
                    terms_note=request.form.get("terms_note", ""),
                    storage_permitted=bool(request.form.get("storage_permitted")),
                    export_permitted=bool(request.form.get("export_permitted")),
                )
                c = report.counts
                flash(f"Imported: {c['created']} new, {c['merged']} merged into existing, "
                      f"{c['suppressed']} skipped (opted out), {c['rejected']} rejected.", "info")
            except (PermissionError, ValueError) as exc:
                flash(str(exc), "error")
    return render_template("import.html", report=report, batches=list(repo.batches().values()),
                           identity_fields=IDENTITY_FIELDS)


@bp.get("/duplicates")
def duplicates():
    return render_template("duplicates.html", pairs=get_repo().possible_duplicates())


@bp.post("/duplicates/merge")
def merge():
    repo = get_repo()
    try:
        keep, drop = int(request.form["keep_id"]), int(request.form["drop_id"])
        repo.merge(keep, drop)
        flash(f"Merged prospect #{drop} into #{keep}.", "info")
    except (KeyError, ValueError) as exc:
        flash(f"Merge failed: {exc}", "error")
    return redirect(url_for("pf.duplicates"))


@bp.get("/export.csv")
def export_csv():
    repo = get_repo()
    body = export_prospects_csv(repo, all_prospects(repo))
    stamp = utcnow().strftime("%Y%m%d")
    return Response(body, mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename=prospects-{stamp}.csv"})


@bp.get("/export/suppression.csv")
def export_suppression():
    body = export_suppression_csv(get_repo())
    return Response(body, mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=suppression-list.csv"})


@bp.get("/rubric")
def rubric():
    return render_template("rubric.html", signals=SIGNALS, groups=GROUPS, policy=DEFAULT_POLICY,
                           sources=policies(cfg().google_places_cache_days))


# ---------------------------------------------------------------- website scan and audit


def google_client():
    key = cfg().google_places_api_key
    return google_places.GooglePlacesClient(key) if key else None


@bp.post("/prospects/<int:business_id>/scan")
def scan(business_id: int):
    repo, b = _load(business_id)
    try:
        result = scan_business(repo, b, make_fetcher(cfg()), cfg(), google_client())
    except google_places.GooglePlacesError as exc:
        flash(f"Scan not run: {exc}", "error")
        return redirect(url_for("pf.prospect", business_id=business_id))
    messages = {
        "ok": "Website scanned",
        "blocked": "Scan stopped: access restricted (not bypassed)",
        "unreachable": "Website did not load on two attempts",
        "no_website": "No website on record to scan",
    }
    flash(f"{messages.get(result.status, result.status)} — {result.detail}",
          "info" if result.status == "ok" else "error")
    return redirect(url_for("pf.prospect", business_id=business_id))


def _audit_for(business_id: int):
    repo, b = _load(business_id)
    p = score_business(repo, b)
    return p, build_audit(b, p.card, opted_out=bool(p.outreach.get("opted_out")), sender=cfg().audit_signature)


@bp.get("/prospects/<int:business_id>/audit")
def audit(business_id: int):
    try:
        p, result = _audit_for(business_id)
    except AuditRefused as exc:
        flash(str(exc), "error")
        return redirect(url_for("pf.prospect", business_id=business_id))
    return render_template("audit.html", p=p, b=p.business, audit=result)


@bp.post("/prospects/<int:business_id>/audit/rewrite")
def audit_rewrite(business_id: int):
    try:
        p, result = _audit_for(business_id)
    except AuditRefused as exc:
        flash(str(exc), "error")
        return redirect(url_for("pf.prospect", business_id=business_id))
    if cfg().anthropic_api_key:
        result = polish_with_claude(result, cfg().anthropic_api_key, cfg().audit_llm_model)
    return render_template("audit.html", p=p, b=p.business, audit=result)


# ---------------------------------------------------------------- Google Places and OpenStreetMap


@bp.post("/import/google")
def google_search():
    client = google_client()
    if client is None:
        flash("Set GOOGLE_PLACES_API_KEY to search Google Places.", "error")
        return redirect(url_for("pf.import_page"))
    query = request.form.get("query", "").strip()
    if not query:
        flash("Enter a search such as 'roofing contractor in Tulsa, OK'.", "error")
        return redirect(url_for("pf.import_page"))
    try:
        results, next_token = client.text_search(query, request.form.get("page_token") or None)
    except google_places.GooglePlacesError as exc:
        flash(str(exc), "error")
        return redirect(url_for("pf.import_page"))
    repo = get_repo()
    return render_template("import.html", report=None, batches=list(repo.batches().values()),
                           identity_fields=IDENTITY_FIELDS, google_results=results, google_query=query,
                           google_next=next_token)


@bp.post("/import/google/save")
def google_import():
    places = []
    for raw in request.form.getlist("place"):
        try:
            item = json.loads(raw)
        except ValueError:
            continue
        if isinstance(item, dict) and re.fullmatch(r"[A-Za-z0-9_-]+", str(item.get("place_id", ""))):
            places.append({k: (str(item[k]) if item.get(k) is not None else None) for k in
                           ("place_id", "name", "street", "city", "state", "postal_code", "maps_uri")})
    if not places:
        flash("Select at least one place to import.", "error")
        return redirect(url_for("pf.import_page"))
    counts = google_places.import_places(get_repo(), places, request.form.get("query", ""))
    stored = "place IDs only" if cfg().google_places_cache_days == 0 else \
        f"details cached for {cfg().google_places_cache_days} days"
    flash(f"Google Places import ({stored}): {counts['created']} new, {counts['merged']} merged, "
          f"{counts['suppressed']} skipped (opted out).", "info")
    return redirect(url_for("pf.index"))


@bp.post("/import/osm")
def osm_import():
    state = request.form.get("state", "")
    city = request.form.get("city", "").strip() or None
    try:
        records = openstreetmap.search(state, city=city, overpass_url=cfg().overpass_url,
                                       user_agent=cfg().user_agent)
    except openstreetmap.OverpassError as exc:
        flash(str(exc), "error")
        return redirect(url_for("pf.import_page"))
    label = f"{city + ', ' if city else ''}{state.upper()}"
    counts = openstreetmap.import_osm(get_repo(), records, label)
    flash(f"OpenStreetMap ({label}): {len(records)} roofers found; {counts['created']} new, "
          f"{counts['merged']} merged, {counts['suppressed']} skipped (opted out). "
          "Data © OpenStreetMap contributors (ODbL).", "info")
    return redirect(url_for("pf.index"))
