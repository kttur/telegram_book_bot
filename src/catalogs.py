"""OPDS mirrors and a shared transport for feeds, books and covers."""

import json
import logging
import math
import os
import queue
import threading
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit

import feedparser
import requests
from urllib3.exceptions import HTTPError as TransportError


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CatalogProgress:
    server_number: int | None
    server_count: int
    stage: str
    via_tor: bool
    attempt_number: int | None = None
    max_attempts: int | None = None
    active_attempts: int = 1


class CatalogUnavailableError(requests.RequestException):
    """None of the configured alternatives could serve a request."""


class InvalidCatalogResponse(requests.RequestException):
    """A catalog returned an error page instead of the requested content."""


@dataclass
class _Attempt:
    index: int | None
    number: int
    started: float
    deadline: float
    client: object
    cancelled: threading.Event = field(default_factory=threading.Event)
    soft_expired: bool = False
    progress: CatalogProgress | None = None

    def check(self):
        if self.cancelled.is_set() or time.monotonic() >= self.deadline:
            raise requests.Timeout("Catalog attempt reached its hard timeout or was cancelled")


def _origin(url):
    parts = urlsplit(url)
    return parts.scheme.lower(), parts.hostname, parts.port or (443 if parts.scheme == "https" else 80)


def _validate_url(url):
    parts = urlsplit(url)
    if (
        parts.scheme not in {"http", "https"}
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
    ):
        raise ValueError(f"Expected an HTTP(S) URL without credentials: {url}")
    parts.port


class CatalogClient:
    def __init__(self, catalogs, *, tor_proxy_url=None, use_tor=False,
                 timeout=(10, 60), auth=None, soft_timeout=5, hard_timeout=90,
                 max_attempts=None):
        if not isinstance(catalogs, list) or not catalogs:
            raise ValueError("Catalogs must be a non-empty JSON array of URLs")
        self.catalogs = []
        for url in catalogs:
            if not isinstance(url, str) or not url.strip():
                raise ValueError("Each catalog must be a non-empty URL string")
            url = url.strip().rstrip("/")
            _validate_url(url)
            if urlsplit(url).query or urlsplit(url).fragment:
                raise ValueError("Catalog base URLs must not contain queries or fragments")
            if url not in self.catalogs:
                self.catalogs.append(url)
        if tor_proxy_url:
            proxy = urlsplit(tor_proxy_url)
            if proxy.scheme != "socks5h" or not proxy.hostname:
                raise ValueError("TOR_PROXY_URL must use socks5h:// for remote DNS")
            proxy.port
        if use_tor and not tor_proxy_url:
            raise ValueError("OPDS_USE_TOR requires TOR_PROXY_URL")
        if len(timeout) != 2 or any(not math.isfinite(t) or t <= 0 for t in timeout):
            raise ValueError("OPDS timeouts must be positive finite numbers")
        if (not math.isfinite(soft_timeout) or not math.isfinite(hard_timeout)
                or not 0 < soft_timeout < hard_timeout):
            raise ValueError("OPDS timeouts must satisfy 0 < soft timeout < hard timeout")
        if max_attempts is None:
            max_attempts = len(self.catalogs)
        if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or max_attempts < 1:
            raise ValueError("OPDS_MAX_ATTEMPTS must be a positive integer")
        self.tor_proxy_url = tor_proxy_url
        self.use_tor = use_tor
        self.timeout = timeout
        self.auth = auth
        self.soft_timeout = soft_timeout
        self.hard_timeout = hard_timeout
        self.max_attempts = max_attempts
        self._active_index = 0
        self._sessions = {}
        self._visited_origins = set()
        self._origins = {_origin(url) for url in self.catalogs}

    @classmethod
    def from_env(cls):
        default_path = Path(__file__).resolve().parent.parent / "catalogs.json"
        path = Path(os.getenv("OPDS_CATALOGS_FILE", str(default_path)))
        with path.open(encoding="utf-8") as config:
            catalogs = json.load(config)
        user, password = os.getenv("OPDS_USER"), os.getenv("OPDS_PASS")
        use_tor = os.getenv("OPDS_USE_TOR", "false").lower()
        if use_tor not in {"true", "false", "1", "0"}:
            raise ValueError("OPDS_USE_TOR must be true, false, 1 or 0")
        return cls(
            catalogs,
            tor_proxy_url=os.getenv("TOR_PROXY_URL") or None,
            use_tor=use_tor in {"true", "1"},
            timeout=(float(os.getenv("OPDS_CONNECT_TIMEOUT", "10")),
                     float(os.getenv("OPDS_READ_TIMEOUT", "60"))),
            auth=(user, password) if user and password else None,
            soft_timeout=float(os.getenv("OPDS_SOFT_TIMEOUT", "5")),
            hard_timeout=float(os.getenv("OPDS_HARD_TIMEOUT", "90")),
            max_attempts=int(os.environ["OPDS_MAX_ATTEMPTS"])
            if os.getenv("OPDS_MAX_ATTEMPTS") else None,
        )

    @property
    def base_url(self):
        return self.catalogs[self._active_index]

    def _mirror_url(self, url, source, target):
        """Rebase OPDS paths, preserving root-level download/cover paths."""
        parts, old, new = urlsplit(url), urlsplit(source), urlsplit(target)
        old_path, new_path = old.path.rstrip("/"), new.path.rstrip("/")
        path = parts.path
        if path == old_path or path.startswith(old_path + "/"):
            path = new_path + path[len(old_path):]
        return urlunsplit((new.scheme, new.netloc, path, parts.query, parts.fragment))

    def _session(self, url):
        try:
            _validate_url(url)
        except ValueError as exc:
            raise requests.exceptions.InvalidURL(str(exc)) from exc
        onion = urlsplit(url).hostname.lower().endswith(".onion")
        if onion and not self.tor_proxy_url:
            raise requests.ConnectionError("An onion address requires TOR_PROXY_URL")
        key = _origin(url)
        if key not in self._sessions:
            session = requests.Session()
            # Environment proxies/netrc must not bypass Tor or leak OPDS credentials.
            session.trust_env = False
            if self.use_tor or onion:
                session.proxies = {"http": self.tor_proxy_url, "https": self.tor_proxy_url}
            self._sessions[key] = session
        return self._sessions[key]

    def _request(self, url, on_progress=None, stage="Получение каталога", attempt=None):
        # Select transport on every redirect, including clearnet -> onion.
        for _ in range(11):
            if attempt:
                attempt.check()
            session = self._session(url)
            self._visited_origins.add(_origin(url))
            if on_progress:
                origin = _origin(url)
                number = next((i + 1 for i, base in enumerate(self.catalogs)
                               if _origin(base) == origin), None)
                if (attempt is not None and attempt.index is not None
                        and _origin(self.catalogs[attempt.index]) == origin):
                    number = attempt.index + 1
                on_progress(CatalogProgress(number, len(self.catalogs), stage,
                                            bool(session.proxies)))
            timeout = self.timeout
            if attempt:
                remaining = max(0.001, attempt.deadline - time.monotonic())
                timeout = tuple(min(value, remaining) for value in timeout)
            response = session.get(
                url, timeout=timeout, allow_redirects=False, stream=True,
                auth=self.auth if _origin(url) in self._origins else None,
            )
            if attempt:
                try:
                    attempt.check()
                except requests.Timeout:
                    response.close()
                    raise
            if response.is_redirect:
                url = urljoin(response.url, response.headers["Location"])
                response.close()
                continue
            try:
                response.raise_for_status()
                # read1 returns available bytes without waiting to fill a chunk:
                # slow trickles must still reach cancellation/deadline checks.
                chunks = []
                if response._content_consumed:
                    chunks.append(response.content)
                else:
                    while True:
                        if attempt:
                            attempt.check()
                        chunk = response.raw.read1(64 * 1024, decode_content=True)
                        if not chunk:
                            break
                        chunks.append(chunk)
                response._content = b"".join(chunks)
                response._content_consumed = True
                if attempt:
                    attempt.check()
            except TransportError as exc:
                response.close()
                raise requests.RequestException(str(exc)) from exc
            except requests.RequestException:
                response.close()
                raise
            return response
        raise requests.TooManyRedirects("Too many catalog redirects")

    def _fetch_once(self, target, base, decode, on_progress, stage, attempt):
        if base is not None:
            # Cookie bootstrap belongs to the same hard deadline as the resource.
            session = self._session(target)
            session.cookies.clear_expired_cookies()
            if not session.cookies:
                try:
                    self._request(base + "/polka/", on_progress, "Получение cookie", attempt).close()
                except requests.RequestException:
                    attempt.check()
                    logger.debug("Cookie bootstrap failed for %s", base, exc_info=True)
        with self._request(target, on_progress, stage, attempt) as response:
            result = decode(response)
            attempt.check()
            return result

    def _remember_cookies(self, client):
        for origin, session in client._sessions.items():
            # A fork also contains untouched cookie snapshots for other mirrors;
            # copying those back could overwrite a concurrent attempt's updates.
            if origin in self._origins and origin in client._visited_origins:
                base = next(base for base in self.catalogs if _origin(base) == origin)
                self._session(base).cookies = session.cookies.copy()

    def _fetch(self, url, decode, on_progress=None, stage="Получение каталога"):
        url = urljoin(self.base_url + "/", url)
        source = next((base for base in self.catalogs if _origin(base) == _origin(url)), None)
        # External resources keep their URL, with the same retry/deadline budget.
        order = ([(self._active_index + offset) % len(self.catalogs)
                  for offset in range(len(self.catalogs))] if source else [None])
        events = queue.Queue()
        active = {}
        started = 0
        next_position = 0
        launch_next = True
        waiting_on = None
        focus = None
        pending_event = None
        last_error = None

        def work(attempt, target, base):
            report = lambda progress: events.put(("progress", attempt, progress, time.monotonic()))
            try:
                result = attempt.client._fetch_once(target, base, decode, report, stage, attempt)
            except Exception as exc:
                events.put(("error", attempt, exc, time.monotonic()))
            else:
                events.put(("success", attempt, result, time.monotonic()))
            finally:
                attempt.client.close()

        def publish(attempt, progress):
            if on_progress:
                on_progress(replace(progress, attempt_number=attempt.number,
                                    max_attempts=self.max_attempts, active_attempts=len(active)))

        try:
            while True:
                # Process completions before deadlines: a success completed before
                # its deadline remains valid even if this thread wakes up later.
                while True:
                    try:
                        event = pending_event if pending_event is not None else events.get_nowait()
                        pending_event = None
                        kind, attempt, value, completed = event
                    except queue.Empty:
                        break
                    if active.get(attempt.index) is not attempt:
                        continue
                    if kind == "progress":
                        attempt.progress = value
                        # Older concurrent attempts must not move the visible status back.
                        if attempt is focus and waiting_on is None:
                            publish(attempt, value)
                        continue
                    del active[attempt.index]
                    if completed < attempt.deadline:
                        self._remember_cookies(attempt.client)
                        if kind == "success":
                            if attempt.index is not None:
                                self._active_index = attempt.index
                            return value
                        if not isinstance(value, requests.RequestException):
                            raise value
                        last_error = value
                    else:
                        last_error = requests.Timeout("Catalog attempt reached its hard timeout")
                    logger.warning("Catalog attempt %s/%s failed: %s",
                                   attempt.number, self.max_attempts, last_error)
                    attempt.cancelled.set()
                    launch_next = True
                now = time.monotonic()
                for index, attempt in list(active.items()):
                    if now >= attempt.deadline:
                        del active[index]
                        attempt.cancelled.set()
                        last_error = requests.Timeout("Catalog attempt reached its hard timeout")
                        logger.warning("Catalog attempt %s/%s reached hard timeout",
                                       attempt.number, self.max_attempts)
                        launch_next = True
                    elif not attempt.soft_expired and now >= attempt.started + self.soft_timeout:
                        attempt.soft_expired = True
                        launch_next = True
                if launch_next and started < self.max_attempts:
                    index = order[next_position % len(order)]
                    if index not in active:
                        started += 1
                        next_position += 1
                        child = self.fork()
                        now = time.monotonic()
                        attempt = _Attempt(index, started, now, now + self.hard_timeout, child)
                        active[index] = attempt
                        focus = attempt
                        waiting_on = None
                        launch_next = False
                        base = self.catalogs[index] if index is not None else None
                        target = self._mirror_url(url, source, base) if base else url
                        threading.Thread(target=work, args=(attempt, target, base),
                                         name=f"catalog-attempt-{started}", daemon=True).start()
                    elif waiting_on is not active[index]:
                        waiting_on = active[index]
                        target = self.catalogs[index] if index is not None else url
                        via_tor = self.use_tor or urlsplit(target).hostname.endswith(".onion")
                        current = waiting_on.progress or CatalogProgress(
                            index + 1 if index is not None else None,
                            len(self.catalogs), stage, via_tor)
                        publish(waiting_on, replace(current,
                            stage=f"{stage}: ожидание текущей попытки до hard timeout"))
                if started == self.max_attempts and not active:
                    raise CatalogUnavailableError(
                        f"Catalog resource is unavailable after {self.max_attempts} failed attempts"
                    ) from last_error
                deadlines = [attempt.deadline for attempt in active.values()]
                deadlines.extend(attempt.started + self.soft_timeout for attempt in active.values()
                                 if not attempt.soft_expired)
                delay = max(0, min(deadlines) - time.monotonic()) if deadlines else 0
                # A queue wakes the scheduler immediately on success, error or stage change.
                try:
                    pending_event = events.get(timeout=delay)
                except queue.Empty:
                    continue
        finally:
            for attempt in active.values():
                attempt.cancelled.set()

    @staticmethod
    def _parse_feed(response):
        feed = feedparser.parse(response.content, response_headers={
            "content-location": response.url,
            "content-type": response.headers.get("Content-Type", "application/atom+xml"),
        })
        if not feed.version.startswith("atom") or feed.get("bozo"):
            raise InvalidCatalogResponse("Expected a valid Atom OPDS feed")
        for entry in feed.entries:
            for link in entry.get("links", []):
                link["href"] = urljoin(response.url, link["href"])
        return feed

    def get_feed(self, url, *, on_progress=None):
        return self._fetch(url, self._parse_feed, on_progress)

    @staticmethod
    def _parse_file(response):
        content_type = response.headers.get("Content-Type", "").lower()
        prefix = response.content.lstrip()[:256].lower()
        if "text/html" in content_type or prefix.startswith((b"<!doctype html", b"<html")):
            raise InvalidCatalogResponse("Received an HTML error page instead of a file")
        if not response.content:
            raise InvalidCatalogResponse("Received an empty file")
        return response

    def get(self, url, *, on_progress=None, operation="Загрузка файла"):
        return self._fetch(url, self._parse_file, on_progress, operation)

    def fork(self):
        """Independent transport for a background resource request."""
        client = CatalogClient(
            list(self.catalogs), tor_proxy_url=self.tor_proxy_url,
            use_tor=self.use_tor, timeout=self.timeout, auth=self.auth,
            soft_timeout=self.soft_timeout, hard_timeout=self.hard_timeout,
            max_attempts=self.max_attempts,
        )
        client._active_index = self._active_index
        for base in self.catalogs:
            session = self._sessions.get(_origin(base))
            if session is not None:
                client._session(base).cookies = session.cookies.copy()
        return client

    def close(self):
        for session in self._sessions.values():
            session.close()
