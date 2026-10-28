"""A deliberately polite HTTP fetcher for public business websites.

- Obeys robots.txt (RFC 9309). If robots.txt keeps returning server errors the
  site is not crawled; that counts as a temporary failure, not as a finding.
- One request at a time per host, at least ``min_delay`` seconds apart (or the
  site's Crawl-delay if longer). The spacing is shared by every fetcher that uses
  the same ``HostThrottle``, so parallel batch workers stay polite too.
- Retries temporary failures (timeouts, connection errors, HTTP 5xx and 429)
  a limited number of times with exponential backoff, honouring Retry-After up
  to a cap. Permanent failures (TLS errors, 404) are not retried.
- Never bypasses access controls: HTTP 401/403, login redirects and CAPTCHA /
  bot-challenge pages stop the scan and are reported as "blocked".
- Does not run JavaScript, submit forms, or send cookies between sites.
"""

from __future__ import annotations

import random
import re
import threading
import time
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from urllib import robotparser
from urllib.parse import urljoin, urlparse

import requests

CHALLENGE_TITLE = re.compile(r"just a moment|attention required|access denied|security check|captcha|"
                             r"are you a robot|verify you are human|pardon our interruption", re.I)
# Markers that only appear on bot-challenge interstitials (Cloudflare, Imperva, PerimeterX, DataDome).
CHALLENGE_MARKERS = re.compile(r"cf-browser-verification|cf_chl_opt|cf-challenge|_incapsula_resource|"
                               r"px-captcha|captcha-delivery\.com", re.I)
# CAPTCHA widgets also appear on ordinary contact forms, so they only count on a near-empty page.
CAPTCHA_WIDGETS = re.compile(r"g-recaptcha|h-captcha|hcaptcha|recaptcha/api", re.I)
LOGIN_PATH = re.compile(r"/(wp-login\.php|login|signin|sign-in|account/login|auth)\b", re.I)


@dataclass
class FetchResult:
    url: str
    final_url: str | None = None
    status: int | None = None
    text: str | None = None
    content_type: str | None = None
    error: str | None = None
    error_kind: str | None = None   # see TEMPORARY_ERRORS / PERMANENT_ERRORS
    blocked: str | None = None      # why we stopped without reading the page
    redirects: list[str] = field(default_factory=list)
    attempts: int = 0
    retry_after: str | None = None

    @property
    def ok(self) -> bool:
        return self.blocked is None and self.status is not None and 200 <= self.status < 300 and self.text is not None

    @property
    def temporary(self) -> bool:
        """True when the failure says nothing about the website itself and is worth retrying later."""
        return self.error_kind in TEMPORARY_ERRORS

    def describe(self) -> str:
        if self.blocked:
            return f"blocked: {self.blocked}"
        if self.error:
            return self.error
        return f"HTTP {self.status}"


# Failures caused by the network, the server being busy, or our own limits.
TEMPORARY_ERRORS = {"timeout", "connect", "dns", "server_error", "rate_limited", "robots_unavailable", "deadline"}
# Failures that repeat on every attempt.
PERMANENT_ERRORS = {"tls", "client_error", "too_many_redirects", "not_html", "other"}
RETRY_STATUSES = {500, 502, 503, 504}


def _classify(exc: Exception) -> tuple[str, str]:
    """(kind, short human-readable message) for a request exception."""
    text = str(exc)
    lower = text.lower()
    if isinstance(exc, requests.exceptions.SSLError) or "certificate" in lower or "ssl" in lower:
        detail = re.search(r"certificate verify failed[^'\")]*|wrong version number|handshake failure", lower)
        return "tls", "TLS/certificate error" + (f" ({detail.group(0).strip()})" if detail else "")
    if isinstance(exc, requests.exceptions.Timeout):
        return "timeout", "timed out"
    if any(m in lower for m in ("name or service not known", "nodename nor servname", "getaddrinfo",
                                "failed to resolve", "name resolution")):
        return "dns", "DNS lookup failed"
    if isinstance(exc, requests.exceptions.ConnectionError):
        if "refused" in lower:
            return "connect", "connection refused"
        if "reset" in lower:
            return "connect", "connection reset"
        return "connect", "could not connect"
    return "other", text[:120]


def _visible_text_length(html: str) -> int:
    body = re.sub(r"(?is)<(script|style|noscript|template)[^>]*>.*?</\1>", " ", html)
    body = re.sub(r"(?s)<[^>]+>", " ", body)
    return len(re.sub(r"\s+", " ", body).strip())


def challenge_reason(html: str) -> str | None:
    """Detect a bot-challenge / CAPTCHA interstitial (not a normal page that has a reCAPTCHA form)."""
    title = re.search(r"(?is)<title[^>]*>(.*?)</title>", html)
    if title and CHALLENGE_TITLE.search(title.group(1)):
        return f"bot challenge page ('{title.group(1).strip()[:60]}')"
    visible = _visible_text_length(html)
    if CHALLENGE_MARKERS.search(html) and visible < 1500:
        return "bot-protection interstitial"
    if CAPTCHA_WIDGETS.search(html) and visible < 150:
        return "CAPTCHA page"
    return None


def _retry_after_seconds(value: str | None) -> float | None:
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        return max(0.0, parsedate_to_datetime(value).timestamp() - time.time())
    except (TypeError, ValueError):
        return None


class HostThrottle:
    """Thread-safe per-host request spacing, shared by all fetchers in a batch."""

    def __init__(self, sleep=time.sleep, clock=time.monotonic):
        self.sleep = sleep
        self.clock = clock
        self._lock = threading.Lock()
        self._next_allowed: dict[str, float] = {}
        self.rate_limited: set[str] = set()

    def wait(self, host: str, delay: float) -> None:
        """Block until ``host`` may be contacted, then reserve the next slot ``delay`` seconds later."""
        with self._lock:
            now = self.clock()
            start = max(now, self._next_allowed.get(host, now))
            self._next_allowed[host] = start + delay
        if start > now:
            self.sleep(start - now)


class PoliteFetcher:
    def __init__(self, user_agent: str, *, min_delay: float = 3.0, timeout: float = 15.0,
                 max_bytes: int = 2_000_000, max_redirects: int = 5, session: requests.Session | None = None,
                 sleep=time.sleep, clock=time.monotonic, max_retries: int = 2, backoff_base: float = 2.0,
                 max_retry_after: float = 30.0, throttle: HostThrottle | None = None, jitter=None):
        self.user_agent = user_agent
        self.min_delay = min_delay
        self.timeout = timeout
        self.max_bytes = max_bytes
        self.max_redirects = max_redirects
        self.session = session or requests.Session()
        self.sleep = sleep
        self.clock = clock
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.max_retry_after = max_retry_after
        self.throttle = throttle or HostThrottle(sleep=sleep, clock=clock)
        self.jitter = jitter or (lambda: random.uniform(0, 0.5))
        self._deadline: float | None = None
        self._robots: dict[str, tuple[robotparser.RobotFileParser | None, str | None, FetchResult | None]] = {}

    def start_site(self, time_limit: float | None) -> None:
        """Begin a new site: requests stop with a temporary 'deadline' error after ``time_limit`` seconds."""
        self._deadline = self.clock() + time_limit if time_limit else None

    # ------------------------------------------------------------ internals

    @staticmethod
    def _origin(url: str) -> str:
        p = urlparse(url)
        return f"{p.scheme}://{p.netloc}"

    def _wait_turn(self, url: str, crawl_delay: float | None = None) -> None:
        self.throttle.wait(urlparse(url).netloc, max(self.min_delay, crawl_delay or 0))

    def _past_deadline(self) -> bool:
        return self._deadline is not None and self.clock() >= self._deadline

    def _get_with_retries(self, url: str, accept: str, crawl_delay: float | None = None) -> FetchResult:
        """One logical request: retries timeouts, connection errors, 5xx and 429 with backoff."""
        attempts = 0
        while True:
            if self._past_deadline():
                return FetchResult(url=url, error="per-site time limit reached", error_kind="deadline",
                                   attempts=attempts)
            self._wait_turn(url, crawl_delay)
            res = self._raw_get(url, accept)
            attempts += 1
            res.attempts = attempts
            retryable = res.error_kind in ("timeout", "connect") or res.status == 429 or (
                res.error_kind == "dns" and attempts == 1) or (
                res.status in RETRY_STATUSES and not (res.text and challenge_reason(res.text)))
            if not retryable or attempts > self.max_retries:
                return res
            wait = self.backoff_base * 2 ** (attempts - 1) + self.jitter()
            if res.status == 429:
                asked = _retry_after_seconds(res.retry_after)
                if asked is not None and asked > self.max_retry_after:
                    return res  # the site wants a longer pause than we are willing to hold a worker for
                wait = max(wait, asked or 0)
            self.sleep(wait)

    def _raw_get(self, url: str, accept: str) -> FetchResult:
        result = FetchResult(url=url)
        try:
            resp = self.session.get(url, headers={"User-Agent": self.user_agent, "Accept": accept},
                                    timeout=(min(5.0, self.timeout), self.timeout), allow_redirects=False,
                                    stream=True)
        except requests.exceptions.RequestException as exc:
            result.error_kind, result.error = _classify(exc)
            return result
        try:
            result.status = resp.status_code
            result.final_url = url
            result.content_type = resp.headers.get("Content-Type", "")
            result.retry_after = resp.headers.get("Retry-After")
            if 300 <= resp.status_code < 400:
                result.redirects = [urljoin(url, resp.headers.get("Location", ""))]
                return result
            chunks, size = [], 0
            for chunk in resp.iter_content(chunk_size=65536):
                chunks.append(chunk)
                size += len(chunk)
                if size >= self.max_bytes:
                    break
            raw = b"".join(chunks)[: self.max_bytes]
            result.text = raw.decode(resp.encoding or "utf-8", errors="replace")
        finally:
            resp.close()
        return result

    def _robots_for(self, url: str):
        origin = self._origin(url)
        if origin not in self._robots:
            res = self._get_with_retries(origin + "/robots.txt", "text/plain")
            hops = 0
            while res.redirects and hops < self.max_redirects and res.error is None:
                target = res.redirects[0]
                hops += 1
                res = self._get_with_retries(target, "text/plain")
            parser, problem = None, None
            if res.error:
                problem = "unreachable"
            elif res.status is not None and (res.status >= 500 or res.status == 429):
                # RFC 9309: an unavailable robots.txt means "do not crawl for now".
                res.error_kind = "rate_limited" if res.status == 429 else "robots_unavailable"
                if res.status == 429:
                    self.throttle.rate_limited.add(urlparse(origin).netloc)
                res.error = f"robots.txt returned HTTP {res.status}; not crawling until it is available"
                problem = "unreachable"
            elif res.status == 200 and res.text is not None:
                parser = robotparser.RobotFileParser()
                parser.parse(res.text.splitlines())
            # Any other 4xx: no robots.txt, crawling allowed.
            if not (problem and res.temporary and res.error_kind != "robots_unavailable"):
                self._robots[origin] = (parser, problem, res)
            else:
                return parser, problem, res  # network trouble: try robots.txt again on the next request
        return self._robots[origin]

    def robots_check(self, url: str) -> tuple[bool, str | None, FetchResult | None, float | None]:
        parser, problem, res = self._robots_for(url)
        if problem == "unreachable":
            return False, None, res, None
        if problem:
            return False, problem, res, None
        if parser is None:
            return True, None, res, None
        allowed = parser.can_fetch(self.user_agent, url)
        delay = parser.crawl_delay(self.user_agent)
        return allowed, None if allowed else "disallowed by robots.txt", res, float(delay) if delay else None

    # ------------------------------------------------------------ public

    def get(self, url: str) -> FetchResult:
        """Fetch an HTML page, following up to ``max_redirects`` redirects (robots checked at each hop)."""
        redirects: list[str] = []
        current = url
        for _ in range(self.max_redirects + 1):
            host = urlparse(current).netloc
            if host in self.throttle.rate_limited:
                return FetchResult(url=url, final_url=current, error="site asked us to slow down (HTTP 429)",
                                   error_kind="rate_limited", redirects=redirects)
            allowed, reason, robots_res, crawl_delay = self.robots_check(current)
            if not allowed and reason is None and robots_res is not None:
                # robots.txt itself could not be read: nothing on this site may be fetched right now.
                return FetchResult(url=url, final_url=current, error=robots_res.error,
                                   error_kind=robots_res.error_kind, redirects=redirects,
                                   attempts=robots_res.attempts)
            if not allowed:
                return FetchResult(url=url, final_url=current, blocked=reason, redirects=redirects)
            res = self._get_with_retries(current, "text/html,application/xhtml+xml", crawl_delay)
            res.url = url
            if res.redirects and res.error is None:
                target = res.redirects[0]
                redirects.append(target)
                if LOGIN_PATH.search(urlparse(target).path):
                    return FetchResult(url=url, final_url=target, status=res.status,
                                       blocked="redirected to a login page", redirects=redirects)
                current = target
                continue
            res.redirects = redirects
            res.final_url = current
            if res.error:
                return res
            if res.status in (401, 407):
                res.blocked, res.text = f"login required (HTTP {res.status})", None
            elif res.status == 403:
                res.blocked, res.text = "access forbidden (HTTP 403)", None
            elif res.status == 429:
                self.throttle.rate_limited.add(host)
                res.error, res.error_kind, res.text = "site asked us to slow down (HTTP 429)", "rate_limited", None
            elif res.text is not None and (res.status == 503 or 200 <= res.status < 300) \
                    and challenge_reason(res.text):
                res.blocked, res.text = challenge_reason(res.text), None
            elif res.status >= 500:
                res.error, res.error_kind, res.text = f"server error (HTTP {res.status})", "server_error", None
            elif res.status >= 400:
                res.error, res.error_kind, res.text = f"HTTP {res.status}", "client_error", None
            if res.text is not None and res.status and 200 <= res.status < 300:
                ctype = (res.content_type or "").lower()
                if ctype and "html" not in ctype:
                    res.error, res.error_kind, res.text = f"content type {ctype} is not HTML", "not_html", None
            return res
        return FetchResult(url=url, final_url=current, error="too many redirects", error_kind="too_many_redirects",
                           redirects=redirects)
