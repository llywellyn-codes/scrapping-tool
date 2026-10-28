"""Bulk website checks: retries, interruptions, duplicate processing, missing websites."""

import re
import sqlite3
import threading
from datetime import timedelta
from pathlib import Path

import pytest
import requests

from prospect_finder import batch, create_app
from prospect_finder.analyzer.fetcher import PoliteFetcher
from prospect_finder.checks import CheckRules, check_state, eligibility
from prospect_finder.config import Config
from prospect_finder.models import Business, EvidenceType, Finding, Outcome, format_ts, utcnow
from prospect_finder.repository import Provenance, Repository, connect, init_db
from prospect_finder.scanner import scan_business
from prospect_finder.service import score_business

from .fake_http import FakeClock, FakeResponse, FakeSession

UA = "RoofingProspectFinder/1.0 (+test)"
PAGE = """<html><head><title>{name} Roofing | Roof Replacement</title>
<meta name="viewport" content="width=device-width"></head><body><h1>{name} Roofing</h1>
<p>Residential roof replacement for homeowners. Serving Dallas and Plano. Licensed and insured.</p>
<a href="tel:+12145550100">Call</a> <a href="/contact">Get a free estimate</a></body></html>"""


def page(name="Acme"):
    return FakeResponse(200, PAGE.format(name=name))


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "batch.db")
    conn = connect(path)
    init_db(conn)
    repo = Repository(conn)
    yield path, repo
    conn.close()


def config(path, **kw):
    return Config(database_path=path, crawler_min_delay_seconds=0, check_site_time_limit=600, **kw)


def factory(session):
    """Fetchers sharing one fake session, each with its own fake clock (no real sleeping)."""
    def make(throttle):
        clock = FakeClock()
        return PoliteFetcher(UA, session=session, sleep=clock.sleep, clock=clock, min_delay=0, throttle=throttle,
                             jitter=lambda: 0, max_retries=2)
    return make


def add(repo, name, website=None):
    fields = {"name": name}
    if website:
        fields["website"] = website
    return repo.create_business(fields, Provenance("manual")).id


SETTINGS = batch.BatchSettings(batch_size=10, concurrency=1)


def fetcher(routes, **kw):
    clock = FakeClock()
    session = FakeSession(routes)
    return PoliteFetcher(UA, session=session, sleep=clock.sleep, clock=clock, min_delay=0, jitter=lambda: 0,
                         **kw), clock, session


# ---------------------------------------------------------------- retries


def test_timeouts_are_retried_with_backoff_then_succeed():
    f, clock, session = fetcher({"https://a.example/": [requests.exceptions.ConnectTimeout("t"),
                                                         requests.exceptions.ReadTimeout("t"), page()]})
    res = f.get("https://a.example/")
    assert res.ok and res.attempts == 3
    assert clock.sleeps == [2.0, 4.0]  # exponential backoff, no jitter in tests


def test_gives_up_after_max_retries_and_marks_failure_temporary():
    f, _, session = fetcher({"https://a.example/": requests.exceptions.ReadTimeout("t")})
    res = f.get("https://a.example/")
    assert not res.ok and res.error_kind == "timeout" and res.temporary
    assert res.attempts == 3 and session.count("https://a.example/") == 3


def test_server_errors_are_retried_but_permanent_errors_are_not():
    f, _, _ = fetcher({"https://a.example/": [FakeResponse(503, "busy"), FakeResponse(502, "bad"), page()]})
    assert f.get("https://a.example/").ok
    f, _, session = fetcher({"https://b.example/": FakeResponse(404, "missing")})
    res = f.get("https://b.example/")
    assert res.error_kind == "client_error" and not res.temporary and session.count("https://b.example/") == 1
    f, _, session = fetcher({"https://c.example/": requests.exceptions.SSLError("certificate verify failed")})
    res = f.get("https://c.example/")
    assert res.error_kind == "tls" and session.count("https://c.example/") == 1


def test_rate_limit_waits_for_retry_after_within_a_cap():
    f, clock, _ = fetcher({"https://a.example/": [FakeResponse(429, "slow", {"Retry-After": "7"}), page()]})
    assert f.get("https://a.example/").ok
    assert clock.sleeps == [7.0]
    f, clock, session = fetcher({"https://b.example/": FakeResponse(429, "slow", {"Retry-After": "600"})})
    res = f.get("https://b.example/")
    assert res.error_kind == "rate_limited" and res.temporary and session.count("https://b.example/") == 1
    assert f.get("https://b.example/other").error_kind == "rate_limited"  # no more requests to that host
    assert session.count("https://b.example/other") == 0


def test_per_site_time_limit_stops_retrying():
    f, _, session = fetcher({"https://a.example/": requests.exceptions.ReadTimeout("t")})
    f.start_site(5)
    res = f.get("https://a.example/")
    assert res.error_kind == "deadline" and res.temporary
    assert session.count("https://a.example/") == 2  # t=0 and t=2; the third try would start at t=6


# ---------------------------------------------------------------- a failed request is not evidence


def test_failed_check_saves_no_findings_and_awards_no_points(db):
    path, repo = db
    bid = add(repo, "Down Roofing", "https://down.example/")
    f, _, _ = fetcher({})
    result = scan_business(repo, repo.get_business(bid), f, config(path))
    assert result.status == "failed"
    assert repo.findings_for(bid) == []
    check = repo.website_check(bid)
    assert check["status"] == "failed" and check["consecutive_failures"] == 1 and check["checked_at"] is None
    p = score_business(repo, repo.get_business(bid))
    assert p.card.total == 0 and p.check_state == "failed"
    assert p.card.result("no_working_website").state.value == "unknown"
    assert p.card.next_step.startswith("The last website check failed")


def test_failed_recheck_keeps_earlier_evidence(db):
    path, repo = db
    bid = add(repo, "Acme", "https://acme.example/")
    f, _, _ = fetcher({"https://acme.example/": page()})
    assert scan_business(repo, repo.get_business(bid), f, config(path)).status == "ok"
    before = len(repo.findings_for(bid))
    first_checked = repo.website_check(bid)["checked_at"]
    f, _, _ = fetcher({"https://acme.example/": FakeResponse(503, "down")})
    assert scan_business(repo, repo.get_business(bid), f, config(path)).status == "failed"
    assert len(repo.findings_for(bid)) == before
    check = repo.website_check(bid)
    assert check["status"] == "failed" and check["checked_at"] == first_checked


# ---------------------------------------------------------------- missing websites


def test_prospects_without_a_website_are_skipped_not_failed(db):
    path, repo = db
    no_site = add(repo, "No Site Roofing")
    with_site = add(repo, "Acme", "https://acme.example/")
    job_id = batch.create_job(repo, SETTINGS)
    progress = batch.job_progress(repo, job_id)
    assert [i["business_id"] for i in progress["items"]] == [with_site]
    assert batch.get_job(repo, job_id)["extra"]["left_out"] == {"no_website": 1}
    batch.run_job(path, job_id, config(path), SETTINGS, fetcher_factory=factory(FakeSession(
        {"https://acme.example/": page()})))
    # Selecting it explicitly records a skip with the reason, never a failure.
    job2 = batch.create_job(repo, SETTINGS, mode="selected", selected_ids=[no_site])
    item = batch.job_progress(repo, job2)["items"][0]
    assert item["state"] == "skipped" and item["result"] == "no_website"
    assert repo.findings_for(no_site) == [] and repo.website_check(no_site) is None
    assert batch.get_job(repo, job2)["status"] == "completed"


def test_website_removed_after_queueing_is_skipped_at_processing_time(db):
    path, repo = db
    bid = add(repo, "Acme", "https://acme.example/")
    job_id = batch.create_job(repo, SETTINGS)
    repo.conn.execute("UPDATE businesses SET website = NULL WHERE id = ?", (bid,))
    repo.conn.commit()
    session = FakeSession({"https://acme.example/": page()})
    progress = batch.run_job(path, job_id, config(path), SETTINGS, fetcher_factory=factory(session))
    assert progress["counts"]["skipped"] == 1 and session.requests == []


# ---------------------------------------------------------------- interrupted batches


def _age_heartbeat(repo, job_id, seconds=120):
    repo.conn.execute("UPDATE check_jobs SET heartbeat_at = ? WHERE id = ?",
                      (format_ts(utcnow() - timedelta(seconds=seconds)), job_id))
    repo.conn.commit()


def test_interrupted_batch_resumes_without_redoing_finished_work(db):
    path, repo = db
    ids = [add(repo, f"Roofer {i}", f"https://r{i}.example/") for i in range(3)]
    session = FakeSession({f"https://r{i}.example/": page(f"R{i}") for i in range(3)})
    cfg = config(path)
    job_id = batch.create_job(repo, SETTINGS)
    job = batch.get_job(repo, job_id)
    make = factory(session)
    f = make(None)
    # First item finishes and is saved; the second is claimed, then the process "dies".
    item_id, bid = batch.claim_next(repo, job_id)
    assert batch.process_item(repo, job, item_id, bid, f, cfg, SETTINGS) == "completed"
    batch.claim_next(repo, job_id)
    _age_heartbeat(repo, job_id)

    assert batch.expire_stale_jobs(repo) == [job_id]
    progress = batch.job_progress(repo, job_id)
    assert progress["status"] == "interrupted"
    assert progress["counts"] == {**progress["counts"], "completed": 1, "processing": 0, "queued": 2}
    assert len(repo.findings_for(ids[0])) > 0  # the finished result survived

    batch.resume_job(repo, job_id)
    progress = batch.run_job(path, job_id, cfg, SETTINGS, fetcher_factory=make)
    assert progress["status"] == "completed" and progress["counts"]["completed"] == 3
    for i in range(3):
        assert session.count(f"https://r{i}.example/") == 1  # nobody was checked twice
    runs = {i["business_id"]: i["attempts"] for i in progress["items"]}
    assert runs == {ids[0]: 1, ids[1]: 2, ids[2]: 1}


def test_result_saved_just_before_a_crash_is_reused_on_resume(db):
    path, repo = db
    bid = add(repo, "Acme", "https://acme.example/")
    session = FakeSession({"https://acme.example/": page()})
    cfg = config(path)
    job_id = batch.create_job(repo, SETTINGS)
    batch.claim_next(repo, job_id)
    scan_business(repo, repo.get_business(bid), factory(session)(None), cfg)  # saved, item not yet marked done
    _age_heartbeat(repo, job_id)
    batch.expire_stale_jobs(repo)
    batch.resume_job(repo, job_id)
    progress = batch.run_job(path, job_id, cfg, SETTINGS, fetcher_factory=factory(session))
    item = progress["items"][0]
    assert item["state"] == "completed" and "already checked during this batch" in item["detail"]
    assert session.count("https://acme.example/") == 1


def test_running_job_with_a_live_heartbeat_is_not_interrupted(db):
    path, repo = db
    add(repo, "Acme", "https://acme.example/")
    job_id = batch.create_job(repo, SETTINGS)
    assert batch.expire_stale_jobs(repo) == []
    assert batch.get_job(repo, job_id)["status"] == "running"


def test_stop_leaves_the_rest_queued_and_resume_finishes(db):
    path, repo = db
    for i in range(3):
        add(repo, f"Roofer {i}", f"https://r{i}.example/")
    session = FakeSession({f"https://r{i}.example/": page() for i in range(3)})
    job_id = batch.create_job(repo, SETTINGS)
    batch.request_stop(repo, job_id)
    progress = batch.run_job(path, job_id, config(path), SETTINGS, fetcher_factory=factory(session))
    assert progress["status"] == "stopped" and progress["counts"]["queued"] == 3
    batch.resume_job(repo, job_id)
    progress = batch.run_job(path, job_id, config(path), SETTINGS, fetcher_factory=factory(session))
    assert progress["status"] == "completed" and progress["counts"]["completed"] == 3


# ---------------------------------------------------------------- duplicate processing


def test_only_one_batch_can_run_at_a_time(db):
    path, repo = db
    add(repo, "Acme", "https://acme.example/")
    add(repo, "Other", "https://other.example/")
    first = batch.create_job(repo, batch.BatchSettings(batch_size=1))
    with pytest.raises(batch.BatchBusy) as exc:
        batch.create_job(repo, SETTINGS)
    assert exc.value.job_id == first
    # The database itself refuses a second active job, even if the check above were bypassed.
    with pytest.raises(sqlite3.IntegrityError):
        repo.conn.execute("INSERT INTO check_jobs (mode, status, batch_size, created_at) VALUES "
                          "('unevaluated', 'running', 1, '2026-01-01')")
    repo.conn.rollback()


def test_claims_are_atomic_across_connections_and_threads(db):
    path, repo = db
    for i in range(20):
        add(repo, f"Roofer {i}", f"https://r{i}.example/")
    job_id = batch.create_job(repo, batch.BatchSettings(batch_size=20))
    claimed, lock = [], threading.Lock()

    def claimer():
        conn = connect(path)
        r = Repository(conn)
        while True:
            got = batch.claim_next(r, job_id)
            if not got:
                break
            with lock:
                claimed.append(got[0])
        conn.close()

    threads = [threading.Thread(target=claimer) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(claimed) == 20 and len(set(claimed)) == 20


def test_parallel_workers_check_each_prospect_exactly_once(db):
    path, repo = db
    for i in range(8):
        add(repo, f"Roofer {i}", f"https://r{i}.example/")
    session = FakeSession({f"https://r{i}.example/": page(f"R{i}") for i in range(8)})
    settings = batch.BatchSettings(batch_size=10, concurrency=4)
    job_id = batch.create_job(repo, settings)
    progress = batch.run_job(path, job_id, config(path), settings, fetcher_factory=factory(session))
    assert progress["counts"]["completed"] == 8
    assert all(session.count(f"https://r{i}.example/") == 1 for i in range(8))
    assert all(c["status"] == "ok" for c in repo.all_website_checks().values())


def test_selected_duplicates_are_checked_once_and_fresh_ones_skipped_unless_forced(db):
    path, repo = db
    a = add(repo, "Acme", "https://acme.example/")
    b = add(repo, "Beta", "https://beta.example/")
    session = FakeSession({"https://acme.example/": page(), "https://beta.example/": page("Beta")})
    scan_business(repo, repo.get_business(b), factory(session)(None), config(path))  # b is now fresh
    job_id = batch.create_job(repo, SETTINGS, mode="selected", selected_ids=[a, a, b])
    items = {i["business_id"]: i for i in batch.job_progress(repo, job_id)["items"]}
    assert set(items) == {a, b}
    assert items[a]["state"] == "queued" and items[b]["state"] == "skipped" and items[b]["result"] == "fresh"
    batch.run_job(path, job_id, config(path), SETTINGS, fetcher_factory=factory(session))
    forced = batch.create_job(repo, SETTINGS, mode="selected", selected_ids=[b], force=True)
    assert batch.job_progress(repo, forced)["items"][0]["state"] == "queued"


def test_a_second_run_does_not_recheck_completed_prospects(db):
    path, repo = db
    add(repo, "Acme", "https://acme.example/")
    session = FakeSession({"https://acme.example/": page()})
    job_id = batch.create_job(repo, SETTINGS)
    batch.run_job(path, job_id, config(path), SETTINGS, fetcher_factory=factory(session))
    with pytest.raises(batch.NothingToCheck):
        batch.create_job(repo, SETTINGS)
    assert session.count("https://acme.example/") == 1


def test_individual_check_during_a_batch_is_not_repeated_by_the_batch(db):
    path, repo = db
    bid = add(repo, "Acme", "https://acme.example/")
    session = FakeSession({"https://acme.example/": page()})
    job_id = batch.create_job(repo, SETTINGS)
    scan_business(repo, repo.get_business(bid), factory(session)(None), config(path))  # the "Check website" button
    progress = batch.run_job(path, job_id, config(path), SETTINGS, fetcher_factory=factory(session))
    assert progress["counts"]["completed"] == 1 and session.count("https://acme.example/") == 1


# ---------------------------------------------------------------- eligibility, batch size, summary


def test_stale_and_failed_retry_rules():
    now = utcnow()
    b = Business(id=1, name="Acme", website="https://acme.example/")
    rules = CheckRules(failed_retry_hours=24, max_failed_attempts=3, stale_after_days=90)
    ok = lambda days: {"status": "ok", "checked_at": now - timedelta(days=days),  # noqa: E731
                       "last_attempt_at": now - timedelta(days=days), "consecutive_failures": 0}
    failed = lambda hours, n=1: {"status": "failed", "checked_at": None,  # noqa: E731
                                 "last_attempt_at": now - timedelta(hours=hours), "consecutive_failures": n}
    assert check_state(None, now) == "unevaluated"
    assert check_state(ok(10), now, rules) == "evaluated" and not eligibility(b, ok(10), False, now, rules=rules).eligible
    assert check_state(ok(100), now, rules) == "stale" and eligibility(b, ok(100), False, now, rules=rules).eligible
    assert eligibility(b, failed(1), False, now, rules=rules).reason == "failed_recently"
    assert eligibility(b, failed(25), False, now, rules=rules).eligible
    assert eligibility(b, failed(25, n=3), False, now, rules=rules).reason == "failed_too_often"
    assert eligibility(b, failed(1, n=3), False, now, selected=True, rules=rules).eligible
    assert eligibility(b, None, True, now).reason == "opted_out"


def test_batch_size_caps_the_queue_and_summary_reports_remaining(db):
    path, repo = db
    for i in range(5):
        add(repo, f"Roofer {i}", f"https://r{i}.example/")
    add(repo, "Down", "https://down.example/")
    routes = {f"https://r{i}.example/": page() for i in range(5)}
    settings = batch.BatchSettings(batch_size=4, concurrency=2)
    job_id = batch.create_job(repo, settings)
    assert batch.job_progress(repo, job_id)["counts"]["queued"] == 4
    progress = batch.run_job(path, job_id, config(path), settings, fetcher_factory=factory(FakeSession(routes)))
    s = progress["summary"]
    assert (s["evaluated"], s["failed"], s["skipped"]) == (4, 0, 0)
    assert s["remaining_unevaluated"] == 2
    job2 = batch.create_job(repo, settings)
    progress = batch.run_job(path, job2, config(path), settings, fetcher_factory=factory(FakeSession(routes)))
    s = progress["summary"]
    assert s["evaluated"] + s["failed"] == 2 and s["failed"] == 1  # down.example fails: not evidence
    assert s["remaining_unevaluated"] == 0
    assert s["left_out"] == {}  # what was left out when this batch was created: nothing
    with pytest.raises(batch.NothingToCheck):  # down.example now waits before its automatic retry
        batch.create_job(repo, settings)


def test_unexpected_error_on_one_prospect_does_not_stop_the_batch(db, monkeypatch):
    path, repo = db
    bad = add(repo, "Bad", "https://bad.example/")
    add(repo, "Good", "https://good.example/")
    real = batch.scan_business

    def flaky(repo_, b, *a, **kw):
        if b.id == bad:
            raise RuntimeError("parser exploded")
        return real(repo_, b, *a, **kw)

    monkeypatch.setattr(batch, "scan_business", flaky)
    job_id = batch.create_job(repo, SETTINGS)
    progress = batch.run_job(path, job_id, config(path), SETTINGS,
                             fetcher_factory=factory(FakeSession({"https://good.example/": page()})))
    assert progress["counts"]["failed"] == 1 and progress["counts"]["completed"] == 1
    assert "parser exploded" in next(i["detail"] for i in progress["items"] if i["business_id"] == bad)


# ---------------------------------------------------------------- migration of existing databases


def test_migration_backs_up_and_keeps_data_but_stops_counting_failed_requests(tmp_path):
    path = str(tmp_path / "old.db")
    conn = connect(path)
    init_db(conn)
    repo = Repository(conn)
    gone = add(repo, "Gone Roofing", "https://gone.example/")
    ok = add(repo, "Fine Roofing", "https://fine.example/")
    repo.update_outreach(gone, status="contacted", last_contact_date="2026-09-01", response="call later")
    now = utcnow()
    legacy = Finding("no_working_website", Outcome.MET, EvidenceType.CONFIRMED_FAILURE, 0.8, "website_scan", now,
                     "https://gone.example/", "Website did not load on two attempts.", "homepage load",
                     "connect: refused")
    tls = Finding("no_https", Outcome.MET, EvidenceType.CONFIRMED_FAILURE, 0.85, "website_scan", now,
                  "https://fine.example/", "https failed", "https load", "https: TLS/certificate error; http: HTTP 200")
    repo.add_finding(gone, legacy)
    repo.add_finding(ok, tls)
    repo.log_scan(gone, now, "unreachable", "connection refused")
    repo.log_scan(ok, now, "ok", "checked 2 page(s)")
    # Make it look like a database from before this version.
    conn.executescript("DELETE FROM website_checks; PRAGMA user_version = 0;")
    conn.close()

    conn = connect(path)
    init_db(conn)
    repo = Repository(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
    assert list(Path(tmp_path).glob("old.db.backup-v0-*"))
    assert repo.website_check(gone)["status"] == "failed"
    assert repo.website_check(ok)["status"] == "ok" and repo.website_check(ok)["checked_at"] is not None
    converted = repo.findings_for(gone)[0]
    assert converted.outcome == Outcome.UNKNOWN and "Website did not load on two attempts." in converted.observation
    assert repo.findings_for(ok)[0].outcome == Outcome.MET  # a real certificate failure stays
    assert repo.get_outreach(gone)["status"] == "contacted" and repo.get_outreach(gone)["response"] == "call later"
    assert score_business(repo, repo.get_business(gone)).card.total == 0
    # Running the migration again changes nothing.
    init_db(conn)
    assert len(list(Path(tmp_path).glob("old.db.backup-*"))) == 1
    conn.close()


# ---------------------------------------------------------------- web


@pytest.fixture
def client(tmp_path):
    path = str(tmp_path / "web.db")
    app = create_app(config(path))
    app.testing = True
    session = FakeSession({"https://acme.example/": page(), "https://beta.example/": page("Beta")})
    app.extensions["pf_fetcher_factory"] = factory(session)
    conn = connect(path)
    repo = Repository(conn)
    ids = [add(repo, "Acme", "https://acme.example/"), add(repo, "Beta", "https://beta.example/"),
           add(repo, "Down", "https://down.example/"), add(repo, "No Site")]
    conn.close()
    with app.test_client() as c:
        c.ids, c.session_fake, c.db_path = ids, session, path
        yield c


def _token(client):
    return re.search(r'name="csrf_token" value="([0-9a-f]+)"', client.get("/").get_data(as_text=True)).group(1)


def test_bulk_check_from_the_prospects_page(client):
    page_html = client.get("/").get_data(as_text=True)
    assert "Check unevaluated websites (3 due)" in page_html and "Check selected" in page_html
    resp = client.post("/checks/start", data={"csrf_token": _token(client), "mode": "unevaluated", "batch_size": "10"})
    assert resp.status_code == 302 and "/checks/" in resp.headers["Location"]
    job_id = int(resp.headers["Location"].rsplit("/", 1)[1])
    batch.wait_for(job_id, timeout=30)
    status = client.get(f"/checks/{job_id}/status.json").get_json()
    assert status["finished"] and status["counts"]["completed"] == 2 and status["counts"]["failed"] == 1
    assert status["counts"]["remaining"] == 0
    summary = client.get(f"/checks/{job_id}").get_data(as_text=True)
    assert "Summary" in summary and "not as evidence" in summary
    def names(f):
        return sorted(re.findall(r'aria-label="Select (?!all")([^"]+)"', client.get(f"/?check={f}").get_data(as_text=True)))

    assert names("evaluated") == ["Acme", "Beta"]
    assert names("failed") == ["Down"]
    assert names("unevaluated") == ["No Site"]
    assert names("stale") == []
    again = client.post("/checks/start", data={"csrf_token": _token(client), "mode": "unevaluated"},
                        follow_redirects=True)
    assert "No prospects need a website check right now." in again.get_data(as_text=True)


def test_check_selected_and_individual_check_still_work(client):
    acme, beta, down, no_site = client.ids
    t = _token(client)
    assert "Tick at least one prospect" in client.post(
        "/checks/start", data={"csrf_token": t, "mode": "selected"}, follow_redirects=True).get_data(as_text=True)
    resp = client.post("/checks/start", data={"csrf_token": t, "mode": "selected", "ids": [str(beta), str(no_site)]})
    job_id = int(resp.headers["Location"].rsplit("/", 1)[1])
    batch.wait_for(job_id, timeout=30)
    status = client.get(f"/checks/{job_id}/status.json").get_json()
    assert status["counts"]["completed"] == 1 and status["counts"]["skipped"] == 1
    assert client.session_fake.count("https://acme.example/") == 0  # not selected, not checked
    page_html = client.post(f"/prospects/{acme}/scan", data={"csrf_token": t}, follow_redirects=True)
    assert "Website checked" in page_html.get_data(as_text=True)
    assert client.session_fake.count("https://acme.example/") == 1
