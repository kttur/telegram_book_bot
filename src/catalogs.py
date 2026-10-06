"""OPDS mirrors and a shared transport for feeds, books and covers."""

import json
import logging
import math
import os
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit

import feedparser
import requests


logger = logging.getLogger(__name__)


class CatalogUnavailableError(requests.RequestException):
    """None of the configured alternatives could serve a request."""


class InvalidCatalogResponse(requests.RequestException):
    """A catalog returned an error page instead of the requested content."""


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
                 timeout=(10, 60), auth=None):
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
        self.tor_proxy_url = tor_proxy_url
        self.use_tor = use_tor
        self.timeout = timeout
        self.auth = auth
        self._active_index = 0
        self._sessions = {}
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

    def _request(self, url):
        # Select transport on every redirect, including clearnet -> onion.
        for _ in range(11):
            session = self._session(url)
            response = session.get(
                url, timeout=self.timeout, allow_redirects=False,
                auth=self.auth if _origin(url) in self._origins else None,
            )
            if response.is_redirect:
                url = urljoin(response.url, response.headers["Location"])
                response.close()
                continue
            try:
                response.raise_for_status()
            except requests.RequestException:
                response.close()
                raise
            return response
        raise requests.TooManyRedirects("Too many catalog redirects")

    def _fetch(self, url, decode):
        url = urljoin(self.base_url + "/", url)
        source = next((base for base in self.catalogs if _origin(base) == _origin(url)), None)
        if source is None:
            # External links are not interchangeable catalog mirrors.
            try:
                with self._request(url) as response:
                    return decode(response)
            except requests.RequestException as exc:
                raise CatalogUnavailableError("External catalog resource is unavailable") from exc
        order = [(self._active_index + offset) % len(self.catalogs)
                 for offset in range(len(self.catalogs))]
        last_error = None
        for index in order:
            base = self.catalogs[index]
            target = self._mirror_url(url, source, base)
            try:
                # Preserve the original /polka/ cookie bootstrap, independently per mirror.
                session = self._session(target)
                session.cookies.clear_expired_cookies()
                if not session.cookies:
                    try:
                        self._request(base + "/polka/").close()
                    except requests.RequestException:
                        logger.debug("Cookie bootstrap failed for %s", base, exc_info=True)
                with self._request(target) as response:
                    result = decode(response)
            except requests.RequestException as exc:
                last_error = exc
                logger.warning("Catalog %s failed: %s", base, exc)
                continue
            if index != self._active_index:
                logger.info("Switching OPDS catalog to %s", base)
            self._active_index = index
            return result
        raise CatalogUnavailableError("All OPDS catalog alternatives are unavailable") from last_error

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

    def get_feed(self, url):
        return self._fetch(url, self._parse_feed)

    @staticmethod
    def _parse_file(response):
        content_type = response.headers.get("Content-Type", "").lower()
        prefix = response.content.lstrip()[:256].lower()
        if "text/html" in content_type or prefix.startswith((b"<!doctype html", b"<html")):
            raise InvalidCatalogResponse("Received an HTML error page instead of a file")
        if not response.content:
            raise InvalidCatalogResponse("Received an empty file")
        return response

    def get(self, url):
        return self._fetch(url, self._parse_file)

    def close(self):
        for session in self._sessions.values():
            session.close()
