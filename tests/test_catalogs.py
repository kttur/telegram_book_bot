import json
import os
import socketserver
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from catalogs import CatalogClient, CatalogUnavailableError


PRIMARY = "https://primary.example/opds"
BACKUP = "https://backup.example/catalog"
ONION = "http://library.onion/opds"
PROXY = "socks5h://tor:9050"
ATOM = b'''<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Catalog</title><id>urn:catalog</id><updated>2026-01-01T00:00:00Z</updated>
  <entry><title>Book</title><id>urn:book</id><updated>2026-01-01T00:00:00Z</updated>
    <link href="authors/1" type="application/atom+xml"/>
    <link href="/b/42/epub" type="application/epub+zip"/>
  </entry>
</feed>'''
EMPTY_ATOM = b'<feed xmlns="http://www.w3.org/2005/Atom"><title>Empty</title></feed>'


def response(url, content=ATOM, status=200, content_type="application/atom+xml", **headers):
    result = requests.Response()
    result.url = url
    result.status_code = status
    result._content = content
    result._content_consumed = True
    result.headers = requests.structures.CaseInsensitiveDict({"Content-Type": content_type, **headers})
    return result


class FakeSession:
    def __init__(self, handler):
        self.handler = handler
        self.trust_env = True
        self.proxies = {}
        self.cookies = requests.cookies.RequestsCookieJar()
        self.cookies.set("session", "test")
        self.calls = []
        self.closed = False

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.handler(url, **kwargs)

    def close(self):
        self.closed = True


class CatalogTests(unittest.TestCase):
    def client(self, handler, catalogs=None, **kwargs):
        sessions = []

        def make_session():
            session = FakeSession(handler)
            sessions.append(session)
            return session

        factory = patch("catalogs.requests.Session", side_effect=make_session)
        factory.start()
        self.addCleanup(factory.stop)
        client = CatalogClient(catalogs or [PRIMARY, BACKUP], **kwargs)
        self.addCleanup(client.close)
        return client, sessions

    def test_network_failure_switches_and_keeps_working_mirror(self):
        calls = []

        def handler(url, **kwargs):
            calls.append(url)
            if url.startswith(PRIMARY):
                raise requests.ConnectTimeout("unavailable")
            return response(url)

        client, sessions = self.client(handler)
        client.get_feed(PRIMARY + '/search?searchTerm=book&pageNumber=2')
        self.assertEqual(client.base_url, BACKUP)
        client.get_feed(PRIMARY + "/authors/1")
        self.assertEqual(calls, [PRIMARY + '/search?searchTerm=book&pageNumber=2',
                                 BACKUP + '/search?searchTerm=book&pageNumber=2',
                                 BACKUP + "/authors/1"])
        self.assertTrue(all(not session.trust_env for session in sessions))
        self.assertEqual(sessions[0].calls[0][1]["timeout"], (10, 60))

    def test_retries_previously_failed_primary_when_backup_fails(self):
        unavailable = {PRIMARY}

        def handler(url, **kwargs):
            if any(url.startswith(base) for base in unavailable):
                return response(url, status=503)
            return response(url)

        client, _ = self.client(handler)
        client.get_feed(PRIMARY)
        unavailable.clear()
        unavailable.add(BACKUP)
        client.get_feed(BACKUP)
        self.assertEqual(client.base_url, PRIMARY)

    def test_html_and_malformed_feed_trigger_fallback(self):
        for content in [b"<html>Blocked</html>", ATOM[:-8]]:
            with self.subTest(content=content):
                client, _ = self.client(lambda url, **kw: response(
                    url, content=content if url.startswith(PRIMARY) else ATOM))
                self.assertEqual(len(client.get_feed(PRIMARY).entries), 1)
                self.assertEqual(client.base_url, BACKUP)

    def test_empty_valid_feed_does_not_switch(self):
        client, sessions = self.client(lambda url, **kw: response(url, EMPTY_ATOM))
        self.assertEqual(client.get_feed(PRIMARY).entries, [])
        self.assertEqual(client.base_url, PRIMARY)
        self.assertEqual(len(sessions), 1)

    def test_relative_links_and_xml_base_use_actual_response_url(self):
        xml = ATOM.replace(b"<entry>", b'<entry xml:base="/alternate/">')

        def handler(url, **kwargs):
            if url.startswith(PRIMARY):
                raise requests.ConnectionError("down")
            return response(url + "/", xml)

        client, _ = self.client(handler)
        links = client.get_feed(PRIMARY).entries[0].links
        self.assertEqual(links[0].href, "https://backup.example/alternate/authors/1")
        self.assertEqual(links[1].href, "https://backup.example/b/42/epub")

    def test_books_and_covers_rebase_root_paths_and_reject_html(self):
        calls = []

        def handler(url, **kwargs):
            calls.append(url)
            if "primary.example" in url:
                return response(url, b"<html>Blocked</html>", content_type="text/html")
            return response(url, b"file bytes", content_type="application/octet-stream")

        client, _ = self.client(handler)
        book = client.get("https://primary.example/b/42/epub?download=1")
        self.assertEqual(book.content, b"file bytes")
        self.assertEqual(book.url, "https://backup.example/b/42/epub?download=1")
        cover = client.get("https://primary.example/covers/42.jpg")
        self.assertEqual(cover.url, "https://backup.example/covers/42.jpg")
        self.assertEqual(len(calls), 3)

    def test_all_failed_error_and_empty_file_fallback(self):
        client, _ = self.client(lambda url, **kw: response(url, b""))
        with self.assertRaises(CatalogUnavailableError):
            client.get(PRIMARY + "/file")
        self.assertEqual(client.base_url, PRIMARY)

    def test_onion_uses_remote_dns_proxy_and_auth(self):
        client, sessions = self.client(lambda url, **kw: response(url),
                                       [ONION], tor_proxy_url=PROXY, auth=("user", "pass"))
        client.get_feed(ONION)
        self.assertEqual(sessions[0].proxies, {"http": PROXY, "https": PROXY})
        self.assertEqual(sessions[0].calls[0][1]["auth"], ("user", "pass"))

    def test_onion_without_tor_never_makes_direct_request(self):
        client, sessions = self.client(lambda url, **kw: response(url), [ONION, PRIMARY])
        client.get_feed(ONION)
        self.assertEqual(client.base_url, PRIMARY)
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].calls[0][0], PRIMARY)
        self.assertEqual(sessions[0].proxies, {})

    def test_only_onion_without_tor_is_unavailable(self):
        client, sessions = self.client(lambda url, **kw: response(url), [ONION])
        with self.assertRaises(CatalogUnavailableError):
            client.get_feed(ONION)
        self.assertEqual(sessions, [])

    def test_all_tor_includes_external_resources_without_opds_credentials(self):
        client, sessions = self.client(lambda url, **kw: response(url, b"cover"),
                                       tor_proxy_url=PROXY, use_tor=True, auth=("user", "pass"))
        client.get("https://images.example/cover.jpg")
        self.assertEqual(client.base_url, PRIMARY)
        self.assertEqual(sessions[0].proxies, {"http": PROXY, "https": PROXY})
        self.assertIsNone(sessions[0].calls[0][1]["auth"])

    def test_clearnet_redirect_to_onion_selects_tor(self):
        def handler(url, **kwargs):
            if url == PRIMARY:
                return response(url, status=302, Location=ONION)
            return response(url)

        client, sessions = self.client(handler, [PRIMARY, ONION], tor_proxy_url=PROXY)
        client.get_feed(PRIMARY)
        self.assertEqual(sessions[0].proxies, {})
        self.assertEqual(sessions[1].proxies, {"http": PROXY, "https": PROXY})
        self.assertTrue(all(call[1]["allow_redirects"] is False
                            for session in sessions for call in session.calls))

    def test_redirect_to_external_host_drops_auth_and_cookies(self):
        def handler(url, **kwargs):
            if url == PRIMARY:
                return response(url, status=302, Location="https://external.example/feed")
            return response(url)

        client, sessions = self.client(handler, auth=("user", "pass"))
        client.get_feed(PRIMARY)
        self.assertIsNone(sessions[1].calls[0][1]["auth"])
        self.assertIsNot(sessions[0].cookies, sessions[1].cookies)

    def test_external_failure_is_not_rebased(self):
        def handler(url, **kwargs):
            raise requests.ConnectionError("unavailable")

        client, sessions = self.client(handler)
        with self.assertRaises(CatalogUnavailableError):
            client.get("https://external.example/image")
        self.assertEqual(len(sessions), 1)
        self.assertEqual(client.base_url, PRIMARY)

    def test_redirect_loop_triggers_fallback(self):
        def handler(url, **kwargs):
            if url.startswith(PRIMARY):
                return response(url, status=302, Location=PRIMARY)
            return response(url)

        client, _ = self.client(handler)
        client.get_feed(PRIMARY)
        self.assertEqual(client.base_url, BACKUP)

    def test_close_closes_all_sessions(self):
        client, sessions = self.client(lambda url, **kw: response(url))
        client.get_feed(PRIMARY)
        client.close()
        self.assertTrue(sessions[0].closed)

    def test_progress_reports_actual_mirror_number_and_transport(self):
        events = []

        def handler(url, **kwargs):
            if url.startswith(PRIMARY):
                raise requests.ConnectTimeout("down")
            return response(url)

        client, _ = self.client(handler, [PRIMARY, ONION], tor_proxy_url=PROXY)
        client.get_feed(PRIMARY, on_progress=events.append)
        self.assertEqual([event.server_number for event in events], [1, 2])
        self.assertEqual([event.via_tor for event in events], [False, True])
        events.clear()
        client.get_feed(PRIMARY, on_progress=events.append)
        self.assertEqual([event.server_number for event in events], [2])

    def test_background_client_has_independent_sessions_cookies_and_mirror_selection(self):
        client, _ = self.client(lambda url, **kw: response(url), tor_proxy_url=PROXY,
                                use_tor=True, auth=("user", "pass"))
        original = client._session(PRIMARY)
        original.cookies.set("mirror", "primary")
        client._active_index = 1
        fork = client.fork()
        self.addCleanup(fork.close)
        copied = fork._session(PRIMARY)
        self.assertIsNot(copied, original)
        self.assertIsNot(copied.cookies, original.cookies)
        self.assertEqual(copied.cookies.get("mirror"), "primary")
        copied.cookies.set("mirror", "changed")
        self.assertEqual(original.cookies.get("mirror"), "primary")
        self.assertEqual(fork.base_url, BACKUP)
        fork._active_index = 0
        self.assertEqual(client.base_url, BACKUP)
        self.assertEqual(fork.auth, client.auth)
        self.assertEqual(fork.timeout, client.timeout)
        self.assertEqual(copied.proxies, {"http": PROXY, "https": PROXY})

    def test_progress_reports_cookie_and_file_stages(self):
        events = []
        client, sessions = self.client(lambda url, **kw: response(url, b"file"))
        client._session(PRIMARY).cookies.clear()
        client.get(PRIMARY + "/file", on_progress=events.append, operation="Загрузка обложки")
        self.assertEqual([event.stage for event in events], ["Получение cookie", "Загрузка обложки"])

    def test_progress_tracks_onion_redirect_and_external_resources(self):
        events = []

        def handler(url, **kwargs):
            if url == PRIMARY:
                return response(url, status=302, Location=ONION)
            return response(url)

        client, _ = self.client(handler, [PRIMARY, ONION], tor_proxy_url=PROXY)
        client.get_feed(PRIMARY, on_progress=events.append)
        self.assertEqual([(event.server_number, event.via_tor) for event in events], [(1, False), (2, True)])
        events.clear()
        client.get("https://external.example/cover", on_progress=events.append)
        self.assertIsNone(events[0].server_number)

    def test_config_validation(self):
        for urls in [[], "https://primary.example", [""], [123], ["ftp://example.org"],
                     ["https://user:pass@example.org"], ["https://example.org/opds?q=1"],
                     ["https://example.org:bad/opds"]]:
            with self.subTest(urls=urls), self.assertRaises(ValueError):
                CatalogClient(urls)
        for kwargs in [{"tor_proxy_url": "socks5://tor:9050"}, {"use_tor": True},
                       {"timeout": (0, 60)}, {"timeout": (10, float("nan"))}]:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                CatalogClient([PRIMARY], **kwargs)

    def test_environment_config(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "catalogs.json"
            config.write_text(json.dumps([PRIMARY, ONION]), encoding="utf-8")
            with patch.dict(os.environ, {"OPDS_CATALOGS_FILE": str(config),
                                         "TOR_PROXY_URL": PROXY, "OPDS_USE_TOR": "true",
                                         "OPDS_USER": "user", "OPDS_PASS": "pass",
                                         "OPDS_CONNECT_TIMEOUT": "12", "OPDS_READ_TIMEOUT": "45"},
                            clear=True):
                client = CatalogClient.from_env()
            self.assertEqual(client.catalogs, [PRIMARY, ONION])
            self.assertEqual(client.timeout, (12, 45))
            self.assertEqual(client.auth, ("user", "pass"))
            self.assertTrue(client.use_tor)


class CookieIntegrationTests(unittest.TestCase):
    def test_real_http_sessions_bootstrap_cookies_per_mirror(self):
        seen = []

        def make_handler(name, unavailable):
            class Handler(BaseHTTPRequestHandler):
                def do_GET(self):
                    seen.append((name, self.path, self.headers.get("Cookie"),
                                 self.headers.get("Authorization")))
                    if self.path == "/opds/polka/":
                        self.send_response(200)
                        self.send_header("Set-Cookie", f"mirror={name}; Path=/")
                        content = b"cookies"
                    elif unavailable:
                        self.send_response(503)
                        content = b"down"
                    else:
                        self.send_response(200)
                        self.send_header("Content-Type", "application/atom+xml")
                        content = ATOM
                    self.end_headers()
                    self.wfile.write(content)

                def log_message(self, *args):
                    pass
            return Handler

        servers = [ThreadingHTTPServer(("127.0.0.1", 0), make_handler("primary", True)),
                   ThreadingHTTPServer(("127.0.0.1", 0), make_handler("backup", False))]
        threads = [threading.Thread(target=server.serve_forever, daemon=True) for server in servers]
        for thread in threads:
            thread.start()
        urls = [f"http://127.0.0.1:{server.server_port}/opds" for server in servers]
        client = CatalogClient(urls, auth=("user", "pass"))
        try:
            self.assertEqual(len(client.get_feed(urls[0]).entries), 1)
            client.get_feed(urls[0] + "/search?searchTerm=book&pageNumber=1")
            self.assertEqual(seen[0][2], None)
            self.assertEqual(seen[1][2], "mirror=primary")
            self.assertEqual(seen[2][2], None)
            self.assertEqual(seen[3][2], "mirror=backup")
            self.assertEqual(seen[4][2], "mirror=backup")
            self.assertEqual(seen[4][1], "/opds/search?searchTerm=book&pageNumber=1")
            self.assertTrue(all(item[3] == "Basic dXNlcjpwYXNz" for item in seen))
        finally:
            client.close()
            for server in servers:
                server.shutdown()
                server.server_close()
            for thread in threads:
                thread.join()


class SocksIntegrationTests(unittest.TestCase):
    def test_onion_hostname_is_sent_to_socks_proxy(self):
        hosts = []
        paths = []

        class SocksHandler(socketserver.StreamRequestHandler):
            def handle(self):
                self.request.settimeout(5)
                version, count = self.rfile.read(2)
                self.rfile.read(count)
                if version != 5:
                    return
                self.wfile.write(b"\x05\x00")
                self.wfile.flush()
                version, command, reserved, address_type = self.rfile.read(4)
                if address_type != 3:
                    return
                length = self.rfile.read(1)[0]
                hosts.append(self.rfile.read(length).decode())
                self.rfile.read(2)
                self.wfile.write(b"\x05\x00\x00\x01\x7f\x00\x00\x01\x00\x50")
                self.wfile.flush()
                request = self.rfile.readline()
                paths.append(request.split()[1].decode())
                while self.rfile.readline().strip():
                    pass
                self.wfile.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: application/atom+xml\r\n"
                    b"Set-Cookie: session=tor; Path=/\r\nConnection: close\r\n"
                    + f"Content-Length: {len(ATOM)}\r\n\r\n".encode() + ATOM
                )

        with socketserver.ThreadingTCPServer(("127.0.0.1", 0), SocksHandler) as proxy:
            thread = threading.Thread(target=proxy.serve_forever, daemon=True)
            thread.start()
            client = CatalogClient([ONION], tor_proxy_url=f"socks5h://127.0.0.1:{proxy.server_address[1]}")
            try:
                self.assertEqual(len(client.get_feed(ONION).entries), 1)
                self.assertEqual(hosts, ["library.onion", "library.onion"])
                self.assertEqual(paths, ["/opds/polka/", "/opds"])
            finally:
                client.close()
                proxy.shutdown()
                thread.join()


if __name__ == "__main__":
    unittest.main()
