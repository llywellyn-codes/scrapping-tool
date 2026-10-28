"""A deliberately polite HTTP fetcher for public business websites.

- Obeys robots.txt (RFC 9309): unreachable robots.txt (5xx) means "do not crawl".
- One request at a time per host, at least ``min_delay`` seconds apart (or the
  site's Crawl-delay if longer).
- Never bypasses access controls: HTTP 401/403/429, login redirects and
  CAPTCHA / bot-challenge pages stop the scan and are reported as "blocked".
- Does not run JavaScript, submit forms, or send cookies between sites.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
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
    error_kind: str | None = None   # dns | connect | timeout | tls | too_many_redirects | not_html | other
    blocked: str | None = None      # why we stopped without reading the page
    redirects: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.blocked is None and self.status is not None and 200 <= self.status < 300 and self.text is not None

    def describe(self) -> str:
        if self.blocked:
            return f"blocked: {self.blocked}"
        if self.error:
            return self.error
        return f"HTTP {self.status}"


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


class PoliteFetcher:
    def __init__(self, user_agent: str, *, min_delay: float = 3.0, timeout: float = 15.0,
                 max_bytes: int = 2_000_000, max_redirects: int = 5, session: requests.Session | None = None,
                 sleep=time.sleep, clock=time.monotonic):
        self.user_agent = user_agent
        self.min_delay = min_delay
        self.timeout = timeout
        self.max_bytes = max_bytes
        self.max_redirects = max_redirects
        self.session = session or requests.Session()
        self.sleep = sleep
        self.clock = clock
        self._last_request: dict[str, float] = {}
        self._robots: dict[str, tuple[robotparser.RobotFileParser | None, str | None, FetchResult | None]] = {}
        self._rate_limited: set[str] = set()

    # ------------------------------------------------------------ internals

    @staticmethod
    def _origin(url: str) -> str:
        p = urlparse(url)
        return f"{p.scheme}://{p.netloc}"

    def _wait_turn(self, url: str, crawl_delay: float | None = None) -> None:
        host = urlparse(url).netloc
        delay = max(self.min_delay, crawl_delay or 0)
        last = self._last_request.get(host)
        if last is not None:
            remaining = delay - (self.clock() - last)
            if remaining > 0:
                self.sleep(remaining)
        self._last_request[host] = self.clock()

    def _raw_get(self, url: str, accept: str) -> FetchResult:
        result = FetchResult(url=url)
        try:
            resp = self.session.get(url, headers={"User-Agent": self.user_agent, "Accept": accept},
                                    timeout=self.timeout, allow_redirects=False, stream=True)
        except requests.exceptions.RequestException as exc:
            result.error_kind, result.error = _classify(exc)
            return result
        try:
            result.status = resp.status_code
            result.final_url = url
            result.content_type = resp.headers.get("Content-Type", "")
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
            self._wait_turn(origin)
            res = self._raw_get(origin + "/robots.txt", "text/plain")
            hops = 0
            while res.redirects and hops < self.max_redirects and res.error is None:
                target = res.redirects[0]
                hops += 1
                self._wait_turn(target)
                res = self._raw_get(target, "text/plain")
            parser, problem = None, None
            if res.error:
                problem = "unreachable"
            elif res.status is not None and res.status >= 500:
                problem = f"robots.txt returned HTTP {res.status}; RFC 9309 treats this as 'do not crawl'"
            elif res.status == 200 and res.text is not None:
                parser = robotparser.RobotFileParser()
                parser.parse(res.text.splitlines())
            # Any other 4xx: no robots.txt, crawling allowed.
            self._robots[origin] = (parser, problem, res)
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
            if host in self._rate_limited:
                return FetchResult(url=url, final_url=current, blocked="site asked us to slow down (HTTP 429)",
                                   redirects=redirects)
            allowed, reason, robots_res, crawl_delay = self.robots_check(current)
            if not allowed and reason is None and robots_res is not None:
                # robots.txt itself was unreachable: the site cannot be reached at all.
                return FetchResult(url=url, final_url=current, error=robots_res.error,
                                   error_kind=robots_res.error_kind, redirects=redirects)
            if not allowed:
                return FetchResult(url=url, final_url=current, blocked=reason, redirects=redirects)
            self._wait_turn(current, crawl_delay)
            res = self._raw_get(current, "text/html,application/xhtml+xml")
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
                self._rate_limited.add(host)
                res.blocked, res.text = "site asked us to slow down (HTTP 429)", None
            elif res.text is not None and (res.status == 503 or 200 <= res.status < 300):
                challenge = challenge_reason(res.text)
                if challenge:
                    res.blocked, res.text = challenge, None
            if res.text is not None and res.status and 200 <= res.status < 300:
                ctype = (res.content_type or "").lower()
                if ctype and "html" not in ctype:
                    res.error, res.error_kind, res.text = f"content type {ctype} is not HTML", "not_html", None
            return res
        return FetchResult(url=url, final_url=current, error="too many redirects", error_kind="too_many_redirects",
                           redirects=redirects)
