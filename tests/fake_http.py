"""A tiny stand-in for requests.Session so website tests never touch the network."""

from __future__ import annotations

import requests


class FakeResponse:
    def __init__(self, status=200, body="", headers=None, encoding="utf-8"):
        self.status_code = status
        self._body = body.encode("utf-8") if isinstance(body, str) else body
        self.headers = {"Content-Type": "text/html; charset=utf-8", **(headers or {})}
        self.encoding = encoding

    def iter_content(self, chunk_size=65536):
        for i in range(0, len(self._body), chunk_size):
            yield self._body[i:i + chunk_size]

    def close(self):
        pass


class FakeSession:
    """Maps URL -> FakeResponse, or -> an exception instance to raise. Unlisted robots.txt = 404."""

    def __init__(self, routes: dict):
        self.routes = routes
        self.requests: list[tuple[str, dict]] = []

    def get(self, url, headers=None, timeout=None, allow_redirects=True, stream=False):
        self.requests.append((url, headers or {}))
        if url not in self.routes:
            if url.endswith("/robots.txt"):
                return FakeResponse(404, "not found", {"Content-Type": "text/plain"})
            raise requests.exceptions.ConnectionError(f"Failed to resolve host for {url} (name resolution)")
        route = self.routes[url]
        if isinstance(route, Exception):
            raise route
        return route


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds
