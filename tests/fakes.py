"""Sessio HTTP i connexio de BD simulades. Cap acces real a xarxa ni a BD."""

from datetime import datetime, timezone

from requests.structures import CaseInsensitiveDict

from app.content_fetch import ContentFetcher, validate_record

FIXED_NOW = datetime(2026, 10, 26, 12, 0, 0, tzinfo=timezone.utc)
HTML_HEADERS = {"Content-Type": "text/html; charset=utf-8"}

LONG_TEXT = ("Aquesta frase d'exemple descriu una mesura de proteccio humana incorporada. " * 12).strip()


class UnexpectedRequest(BaseException):
    """Es llanca (BaseException, perque no la capturi `except Exception`) si el codi demana una URL no prevista."""


class FakeResponse:
    def __init__(self, status=200, body=b"", headers=None, encoding="utf-8", fail_while_reading=None):
        self.status_code = status
        self.headers = CaseInsensitiveDict(headers or {})
        self._body = body.encode("utf-8") if isinstance(body, str) else body
        self.encoding = encoding
        self.closed = False
        self.iterated = False
        self._fail = fail_while_reading

    def iter_content(self, chunk_size=1):
        self.iterated = True
        if self._fail is not None:
            raise self._fail
        for start in range(0, len(self._body), 4096):
            yield self._body[start:start + 4096]

    def close(self):
        self.closed = True


class FakeClock:
    def __init__(self):
        self.t = 0.0
        self.sleeps = []

    def __call__(self):
        return self.t

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.t += seconds


class Call:
    def __init__(self, url, headers, timeout, allow_redirects, stream, at):
        self.url = url
        self.headers = headers
        self.timeout = timeout
        self.allow_redirects = allow_redirects
        self.stream = stream
        self.at = at


class FakeSession:
    """routes: {url: FakeResponse | excepcio | [llista de les anteriors, una per crida]}."""

    def __init__(self, routes, clock=None):
        self.routes = dict(routes)
        self.calls = []
        self._clock = clock

    def get(self, url, headers=None, timeout=None, allow_redirects=None, stream=None):
        self.calls.append(Call(url, headers, timeout, allow_redirects, stream, self._clock() if self._clock else None))
        if url not in self.routes:
            raise UnexpectedRequest(url)
        item = self.routes[url]
        if isinstance(item, list):
            item = item.pop(0) if len(item) > 1 else item[0]
        if isinstance(item, BaseException) or (isinstance(item, type) and issubclass(item, BaseException)):
            raise item
        return item

    @property
    def urls(self):
        return [c.url for c in self.calls]

    def calls_to(self, url):
        return [c for c in self.calls if c.url == url]


def page(title, body):
    return f"<html><head><title>{title}</title></head><body>{body}</body></html>"


def ok_page(url_title="Article", body=None):
    return FakeResponse(200, page(url_title, body if body is not None else f"<article>{LONG_TEXT}</article>"), HTML_HEADERS)


def make_fetcher(routes, **kwargs):
    clock = FakeClock()
    session = FakeSession(routes, clock)
    fetcher = ContentFetcher(session, sleep=clock.sleep, clock=clock, now=lambda: FIXED_NOW, **kwargs)
    return fetcher, session, clock


def fetch(routes, url, **kwargs):
    """Obte `url` amb una sessio simulada i comprova que el registre compleix el contracte de la BD."""
    find_reusable = kwargs.pop("find_reusable", None)
    fetcher, session, clock = make_fetcher(routes, **kwargs)
    record = fetcher.fetch(url, find_reusable=find_reusable)
    validate_record(record)
    return record, session


NULLABLE_FIELDS = (
    "http_status", "final_url", "content_type", "text_content", "text_length",
    "content_hash", "text_source", "error_detail", "reused_from_fetch_id",
)


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.conn.executed.append((sql, params))
        self.conn.last = self.conn.results.pop(0) if self.conn.results else None

    def fetchone(self):
        return self.conn.last


class FakeConn:
    def __init__(self, results=None):
        self.results = list(results or [])
        self.executed = []
        self.last = None
        self.commits = 0
        self.rollbacks = 0
        self.cursors_opened = 0

    def cursor(self):
        self.cursors_opened += 1
        return FakeCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1
