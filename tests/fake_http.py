"""A tiny stand-in for requests.Session so website tests never touch the network."""

from __future__ import annotations

import threading

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
    """Maps URL -> FakeResponse or an exception instance to raise, or -> a list of those returned in turn
    (the last one repeats). Unlisted robots.txt = 404; any other unlisted URL fails DNS resolution."""

    def __init__(self, routes: dict):
        self.routes = routes
        self.requests: list[tuple[str, dict]] = []
        self._lock = threading.Lock()

    def get(self, url, headers=None, timeout=None, allow_redirects=True, stream=False):
        with self._lock:
            self.requests.append((url, headers or {}))
            route = self.routes.get(url)
            if isinstance(route, list):
                route = route.pop(0) if len(route) > 1 else route[0]
        if route is None:
            if url.endswith("/robots.txt"):
                return FakeResponse(404, "not found", {"Content-Type": "text/plain"})
            raise requests.exceptions.ConnectionError(f"Failed to resolve host for {url} (name resolution)")
        if isinstance(route, Exception):
            raise route
        return route

    def count(self, url: str) -> int:
        return sum(1 for u, _ in self.requests if u == url)


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds
