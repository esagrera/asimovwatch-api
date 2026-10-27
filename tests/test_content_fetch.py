"""
Tests de app/content_fetch.py. Sense xarxa ni BD: sessio HTTP simulada.
Cada registre produit es valida amb validate_record() (contracte de la BD).
"""

import hashlib
import re
from datetime import datetime

import pytest
import requests

from app import content_fetch as cf
from app.content_fetch import (
    ACCEPT_HEADER,
    DEFAULT_USER_AGENT,
    FETCH_STATUSES,
    ContentFetcher,
    FetchRecord,
    FetchRecordError,
    clean_text,
    discard_reason_for,
    normalize_url,
    strip_utm_params,
    validate_record,
)
from tests.fakes import (
    FIXED_NOW,
    HTML_HEADERS,
    LONG_TEXT,
    NULLABLE_FIELDS,
    FakeResponse,
    fetch,
    make_fetcher,
    ok_page,
    page,
)

ART = "https://example.com/a"
ROBOTS = "https://example.com/robots.txt"


def robots_404(host="example.com"):
    return {f"https://{host}/robots.txt": FakeResponse(404)}


def routes_for(url=ART, response=None, robots=None):
    routes = {"https://example.com/robots.txt": robots or FakeResponse(404)}
    routes[url] = response if response is not None else ok_page()
    return routes


def assert_no_empty_values(record):
    """Camps sense valor = None: mai "" ni 0."""
    for name in NULLABLE_FIELDS:
        assert getattr(record, name) not in ("", 0), name


# ---------------------------------------------------------------------------
# Vocabulari i mapatges
# ---------------------------------------------------------------------------

def test_vocabulary_is_exactly_the_check():
    assert FETCH_STATUSES == {
        "ok", "robots_blocked", "robots_unreachable", "access_denied", "anti_bot_challenge",
        "text_too_short", "timeout", "http_error", "error", "pdf_unprocessed", "duplicate",
    }
    assert cf.TEXT_SOURCES == {"html_article", "html_main", "html_body"}
    assert cf.EXTRACT_METHOD_TO_TEXT_SOURCE == {
        "article": "html_article", "main": "html_main", "body": "html_body",
    }


def test_no_catalan_vocabulary_in_module_source():
    source = open(cf.__file__, encoding="utf-8").read()
    for old in ("acces_denegat", "text_massa_curt", "bloquejat_per_robots", "repte_anti_bot",
                "error_http", "pdf_sense_processar", "omes_per_decisio", "duplicat_de"):
        assert old not in source, old


@pytest.mark.parametrize("status,expected", [
    ("access_denied", "fetch_failed"),
    ("robots_blocked", "fetch_failed"),
    ("robots_unreachable", "fetch_failed"),
    ("anti_bot_challenge", "fetch_failed"),
    ("timeout", "fetch_failed"),
    ("http_error", "fetch_failed"),
    ("error", "fetch_failed"),
    ("text_too_short", "insufficient_text"),
    ("ok", None),
    ("duplicate", None),
    ("pdf_unprocessed", None),
    ("qualsevol_altre", None),
    (None, None),
])
def test_discard_reason_for(status, expected):
    assert discard_reason_for(status) == expected


def test_discard_reason_values_are_admitted_by_the_entries_check():
    admitted = {"out_of_scope", "no_content", "insufficient_text", "fetch_failed"}
    for status in FETCH_STATUSES:
        assert discard_reason_for(status) in admitted | {None}


# ---------------------------------------------------------------------------
# Estats de l'obtencio
# ---------------------------------------------------------------------------

def test_ok_article_full_record():
    record, session = fetch(routes_for(response=ok_page("Titol", f"<article>{LONG_TEXT}</article>")), ART)
    assert record.fetch_status == "ok"
    assert record.text_source == "html_article"
    assert record.http_status == 200
    assert record.final_url == ART
    assert record.content_type == "text/html; charset=utf-8"
    assert record.text_content == LONG_TEXT
    assert record.text_length == len(record.text_content)
    assert record.truncated is False
    assert record.reused_from_fetch_id is None
    assert record.error_detail is None
    assert record.requested_url == ART and record.normalized_url == ART
    assert record.user_agent == DEFAULT_USER_AGENT
    assert record.fetched_at == FIXED_NOW
    assert_no_empty_values(record)


@pytest.mark.parametrize("body,expected_source", [
    (f"<article>{LONG_TEXT}</article>", "html_article"),
    (f"<main>{LONG_TEXT}</main>", "html_main"),
    (f"<div>{LONG_TEXT}</div>", "html_body"),
])
def test_text_source_mapping(body, expected_source):
    record, _ = fetch(routes_for(response=ok_page("T", body)), ART)
    assert record.fetch_status == "ok"
    assert record.text_source == expected_source


def test_boilerplate_is_removed_and_ties_prefer_the_specific_container():
    body = f"<nav>MENU MENU MENU</nav><main>{LONG_TEXT}</main><footer>PEU</footer>"
    record, _ = fetch(routes_for(response=ok_page("T", body)), ART)
    assert record.text_source == "html_main"  # empat amb body: guanya el contenidor especific
    assert "MENU" not in record.text_content and "PEU" not in record.text_content


# ---------------------------------------------------------------------------
# Eleccio del contenidor: main > article amb mes text > body (NO el mes llarg)
# ---------------------------------------------------------------------------

def container_record(body, **kwargs):
    record, _ = fetch(routes_for(response=ok_page("T", body)), ART, **kwargs)
    return record


def sha256_hex(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_long_main_is_chosen_even_if_body_is_longer():
    body = f"<div>{LONG_TEXT} {LONG_TEXT}</div><main>{LONG_TEXT}</main>"
    record = container_record(body)
    assert record.fetch_status == "ok" and record.text_source == "html_main"
    assert record.text_content == LONG_TEXT and record.error_detail is None
    assert record.text_length == len(LONG_TEXT)


def test_short_main_with_long_body_is_text_too_short_with_the_main_text():
    main_text = "m" * 100
    widget = "w" * 2800
    record = container_record(f"<div>{widget}</div><main>{main_text}</main>")
    assert record.fetch_status == "text_too_short"
    assert record.text_source == "html_main"
    assert record.text_content == main_text and record.text_length == 100       # el text de main, no el de body
    assert record.content_hash == sha256_hex(main_text)
    assert record.error_detail == "container=main;len=100;body_len=2901"          # 2800 + espai + 100


def test_gsa_photos_pattern_tiny_article_plus_widget_article_plus_short_main():
    """gsa.gov/photos: <article> de 5 + <article> de 2471 amb un giny + <main> curt (103)."""
    widget = "g" * 2471
    main_text = "m" * 103
    html = f"<article>Top 1</article><article></article><article>{widget}</article><main>{main_text}</main>"
    record = container_record(html)
    assert record.fetch_status == "text_too_short"
    assert record.text_source == "html_main" and record.text_content == main_text
    assert "g" not in record.text_content                                          # el giny no s'hi cola
    assert record.error_detail == f"container=main;len=103;body_len={5 + 1 + 2471 + 1 + 103}"


def test_without_main_the_article_with_most_text_is_chosen():
    html = f"<div>{'c' * 2000}</div><article>{'a' * 300}</article><article>{'b' * 900}</article>"
    record = container_record(html)
    assert record.fetch_status == "ok" and record.text_source == "html_article"
    assert record.text_content == "b" * 900                                         # ni el primer ni body


def test_equal_articles_pick_the_first():
    record = container_record(f"<article>{'x' * 500}</article><article>{'y' * 500}</article>")
    assert record.text_source == "html_article" and record.text_content == "x" * 500


def test_without_main_or_article_body_is_used():
    record = container_record(f"<div>{LONG_TEXT}</div><section>més</section>")
    assert record.fetch_status == "ok" and record.text_source == "html_body"
    assert record.text_content == f"{LONG_TEXT} més"


def test_short_article_without_main_never_falls_back_to_body():
    record = container_record(f"<div>{LONG_TEXT}</div><article>Top 1</article>")
    assert record.fetch_status == "text_too_short"
    assert record.text_source == "html_article" and record.text_content == "Top 1"
    assert record.error_detail == f"container=article;len=5;body_len={len(LONG_TEXT) + 1 + 5}"


def test_deepmind_pattern_small_articles_and_big_main():
    small = "".join(f"<article>{'a' * n}</article>" for n in (24, 146, 124, 108))
    big = "M" * 9382
    record = container_record(f"{small}<main>{big}</main>")
    assert record.fetch_status == "ok" and record.text_source == "html_main"
    assert record.text_content == big and record.text_length == 9382


def test_empty_main_is_still_the_container_and_has_no_text_fields():
    record = container_record(f"<div>{LONG_TEXT}</div><main></main>")
    assert record.fetch_status == "text_too_short"
    assert record.error_detail == f"container=main;len=0;body_len={len(LONG_TEXT)}"
    for name in ("text_content", "text_length", "content_hash", "text_source"):
        assert getattr(record, name) is None, name


def test_only_the_first_main_counts():
    record = container_record(f"<main>short</main><main>{LONG_TEXT}</main>")
    assert record.fetch_status == "text_too_short" and record.text_content == "short"
    assert record.error_detail.startswith("container=main;len=5;")


def test_main_text_includes_nested_articles():
    record = container_record(f"<main><article>{LONG_TEXT}</article></main>")
    assert record.text_source == "html_main" and record.text_content == LONG_TEXT


def test_main_wins_over_a_larger_article():
    record = container_record(f"<article>{'a' * 800}</article><main>{'m' * 450}</main>")
    assert record.fetch_status == "ok" and record.text_source == "html_main"
    assert record.text_content == "m" * 450


@pytest.mark.parametrize("tag", ["nav", "header", "footer", "aside", "form"])
def test_cleanup_is_unchanged_a_container_inside_a_removed_tag_does_not_exist(tag):
    record = container_record(f"<{tag}><main>{LONG_TEXT}</main></{tag}><div>{LONG_TEXT}</div>")
    assert record.text_source == "html_body"                      # el main esta dins d'una etiqueta que s'elimina
    assert record.text_content == LONG_TEXT


def test_cleanup_is_unchanged_inside_the_chosen_container():
    html = f"<main><nav>MENU</nav>{LONG_TEXT}<footer>PEU</footer><script>x()</script><style>p{{}}</style></main>"
    record = container_record(html)
    assert record.text_content == LONG_TEXT


def test_text_too_short_detail_only_for_that_status_and_threshold_is_inclusive():
    exact = container_record(f"<main>{'m' * 50}</main>", min_text_chars=50)
    below = container_record(f"<main>{'m' * 50}</main>", min_text_chars=51)
    assert exact.fetch_status == "ok" and exact.error_detail is None
    assert below.fetch_status == "text_too_short"
    assert below.error_detail == "container=main;len=50;body_len=50"


def test_extract_page_reports_the_container_and_body_length():
    page_info = cf.extract_page(page("T", f"<div>{'w' * 20}</div><main>abc</main>"))
    assert (page_info.container, page_info.method, page_info.text, page_info.body_len) == ("main", "main", "abc", 24)
    empty = cf.extract_page(page("T", "<div>xx</div><article></article>"))
    assert (empty.container, empty.method, empty.text, empty.body_len) == ("article", None, "", 2)
    body_only = cf.extract_page(page("T", "<p>hola</p>"))
    assert (body_only.container, body_only.method, body_only.body_len) == ("body", "body", 4)


def test_challenge_title_uses_the_length_of_the_chosen_container():
    """El titol de repte nomes aplica si el text EXTRET (el del contenidor triat) es curt."""
    routes = routes_for(response=FakeResponse(200, page("Just a moment", f"<div>{LONG_TEXT}</div><main>wait</main>"), HTML_HEADERS))
    record, _ = fetch(routes, ART)
    assert record.fetch_status == "anti_bot_challenge"
    routes = routes_for(response=FakeResponse(200, page("Just a moment", f"<div>w</div><main>{LONG_TEXT}</main>"), HTML_HEADERS))
    record, _ = fetch(routes, ART)
    assert record.fetch_status == "ok"


def test_text_too_short_keeps_text_and_threshold_is_400():
    assert cf.DEFAULT_MIN_TEXT_CHARS == 400
    record, _ = fetch(routes_for(response=ok_page("T", "<p>text molt curt</p>")), ART)
    assert record.fetch_status == "text_too_short"
    assert record.text_content == "text molt curt"
    assert record.text_length == len("text molt curt")
    assert record.text_source == "html_body"
    assert len(record.content_hash) == 64


def test_threshold_is_configurable_and_boundary_is_inclusive():
    body = "x" * 50
    below, _ = fetch(routes_for(response=ok_page("T", body)), ART, min_text_chars=51)
    exact, _ = fetch(routes_for(response=ok_page("T", body)), ART, min_text_chars=50)
    assert below.fetch_status == "text_too_short"
    assert exact.fetch_status == "ok"


def test_page_without_text_has_no_text_fields():
    html = "<html><body><script>var x = 1;</script></body></html>"
    record, _ = fetch(routes_for(response=FakeResponse(200, html, HTML_HEADERS)), ART)
    assert record.fetch_status == "text_too_short"
    for name in ("text_content", "text_length", "content_hash", "text_source"):
        assert getattr(record, name) is None, name


@pytest.mark.parametrize("code", [401, 402, 403])
def test_access_denied(code):
    resp = FakeResponse(code, "denegat", HTML_HEADERS)
    record, session = fetch(routes_for(response=resp), ART)
    assert record.fetch_status == "access_denied"
    assert record.http_status == code
    assert record.error_detail == f"HTTP {code}"
    assert record.text_content is None and record.content_hash is None
    assert resp.iterated is False  # no es llegeix el cos
    assert len(session.calls_to(ART)) == 1  # cap segon intent


@pytest.mark.parametrize("code", [404, 410, 429, 500, 503])
def test_http_error(code):
    resp = FakeResponse(code, "error", HTML_HEADERS)
    record, session = fetch(routes_for(response=resp), ART)
    assert record.fetch_status == "http_error"
    assert record.http_status == code
    assert resp.iterated is False
    assert len(session.calls_to(ART)) == 1


def test_timeout_on_request():
    record, _ = fetch(routes_for(response=requests.Timeout("lent")), ART)
    assert record.fetch_status == "timeout"
    assert record.http_status is None and record.final_url is None
    assert record.text_content is None


def test_timeout_while_reading_body():
    resp = FakeResponse(200, b"", HTML_HEADERS, fail_while_reading=requests.ReadTimeout("lent"))
    record, _ = fetch(routes_for(response=resp), ART)
    assert record.fetch_status == "timeout"


def test_other_exception_is_error():
    record, _ = fetch(routes_for(response=requests.ConnectionError("sense xarxa")), ART)
    assert record.fetch_status == "error"
    assert "ConnectionError" in record.error_detail
    assert record.http_status is None


def test_pdf_by_content_type_is_not_read():
    resp = FakeResponse(200, b"%PDF-1.4 ...", {"Content-Type": "application/pdf"})
    record, _ = fetch(routes_for(response=resp), ART)
    assert record.fetch_status == "pdf_unprocessed"
    assert record.http_status == 200
    assert record.text_content is None and record.text_source is None and record.content_hash is None
    assert resp.iterated is False


def test_pdf_by_extension():
    url = "https://example.com/doc/report.PDF"
    resp = FakeResponse(200, b"%PDF", HTML_HEADERS)
    record, _ = fetch(routes_for(url=url.rstrip("/"), response=resp), url)
    assert record.fetch_status == "pdf_unprocessed"
    assert resp.iterated is False


@pytest.mark.parametrize("canonical", [None, "", "   "])
def test_no_canonical_url_is_an_error_with_a_fixed_detail(canonical):
    fetcher, session, _ = make_fetcher({})
    record = fetcher.fetch_entry_content(canonical)
    validate_record(record)
    assert record.fetch_status == "error"
    assert record.error_detail == "no_canonical_url"
    assert record.requested_url == "" and record.normalized_url == ""
    assert session.calls == []


@pytest.mark.parametrize("url", ["ftp://example.com/a", "http://[::1", "no es una url", "mailto:a@b.cat"])
def test_error_for_unusable_url_keeps_requested_url(url):
    fetcher, session, _ = make_fetcher({})
    record = fetcher.fetch(url)
    validate_record(record)
    assert record.fetch_status == "error"
    assert record.requested_url == url and record.normalized_url == url
    assert session.calls == []


def test_there_is_no_source_url_fallback():
    fetcher, session, _ = make_fetcher({
        "https://example.com/robots.txt": FakeResponse(404),
        "https://example.com/source": ok_page(),
    })
    # la interficie ja no accepta una URL de la font
    with pytest.raises(TypeError):
        fetcher.fetch_entry_content(None, "https://example.com/source")
    assert not hasattr(cf, "select_requested_url")
    record = fetcher.fetch_entry_content(None)
    assert record.fetch_status == "error" and record.error_detail == "no_canonical_url"
    assert session.calls == []


# ---------------------------------------------------------------------------
# Camps sense valor = None (mai "" ni 0)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("response,expected_none", [
    (requests.Timeout("x"), ("http_status", "final_url", "content_type", "text_content", "text_length",
                             "content_hash", "text_source", "reused_from_fetch_id")),
    (FakeResponse(403), ("text_content", "text_length", "content_hash", "text_source", "reused_from_fetch_id")),
])
def test_absent_fields_are_none(response, expected_none):
    record, _ = fetch(routes_for(response=response), ART)
    for name in expected_none:
        assert getattr(record, name) is None, name
    assert_no_empty_values(record)


def test_duplicate_has_no_text_fields_and_no_network():
    seen = []
    fetcher, session, _ = make_fetcher({})
    record = fetcher.fetch(ART + "/", find_reusable=lambda url: seen.append(url) or 7)
    validate_record(record)
    assert seen == [ART]  # es consulta amb la URL normalitzada
    assert record.fetch_status == "duplicate"
    assert record.reused_from_fetch_id == 7
    for name in ("text_content", "text_length", "content_hash", "text_source", "http_status",
                 "final_url", "content_type", "error_detail"):
        assert getattr(record, name) is None, name
    assert record.truncated is False
    assert session.calls == []


def test_no_reusable_row_means_a_normal_fetch():
    record, session = fetch(routes_for(), ART, find_reusable=lambda url: None)
    assert record.fetch_status == "ok"
    assert ART in session.urls


def test_lookup_errors_are_not_swallowed():
    def boom(url):
        raise RuntimeError("BD caiguda")
    fetcher, _, _ = make_fetcher(routes_for())
    with pytest.raises(RuntimeError):
        fetcher.fetch(ART, find_reusable=boom)


# ---------------------------------------------------------------------------
# Hash, NUL i truncat
# ---------------------------------------------------------------------------

def test_hash_is_full_lowercase_sha256_of_stored_text():
    record, _ = fetch(routes_for(), ART)
    assert re.fullmatch(r"[0-9a-f]{64}", record.content_hash)
    assert record.content_hash == hashlib.sha256(record.text_content.encode("utf-8")).hexdigest()
    assert len(record.content_hash) == 64


def test_nul_characters_are_removed_before_hash_and_length():
    body = f"<p>abc\x00def {LONG_TEXT}</p>"
    record, _ = fetch(routes_for(response=ok_page("T", body)), ART)
    assert "\x00" not in record.text_content
    assert "abcdef" in record.text_content
    assert record.text_length == len(record.text_content)
    assert record.content_hash == hashlib.sha256(record.text_content.encode("utf-8")).hexdigest()


def test_clean_text():
    assert clean_text("a\x00b  c\n\t d") == "ab c d"
    assert clean_text(None) == "" and clean_text("") == ""
    assert clean_text("\ud800x") == "x"  # surrogat no codificable


def test_text_length_counts_characters_not_bytes():
    record, _ = fetch(routes_for(response=ok_page("T", "<p>" + "à" * 450 + "</p>")), ART)
    assert record.text_length == 450
    assert len(record.text_content.encode("utf-8")) == 900


def test_truncated_when_body_exceeds_max_bytes():
    html = page("T", "<p>" + "x" * 5000 + "</p>")
    record, _ = fetch(routes_for(response=FakeResponse(200, html, HTML_HEADERS)), ART,
                      max_bytes=1000, min_text_chars=1)
    assert record.truncated is True


def test_not_truncated_when_body_is_exactly_max_bytes():
    base = page("T", "<p></p>")
    filler = 1000 - len(base.encode("utf-8"))
    html = page("T", "<p>" + "x" * filler + "</p>")
    assert len(html.encode("utf-8")) == 1000
    record, _ = fetch(routes_for(response=FakeResponse(200, html, HTML_HEADERS)), ART,
                      max_bytes=1000, min_text_chars=1)
    assert record.truncated is False


def test_default_max_bytes_is_3_mb():
    assert cf.MAX_BYTES == 3_000_000


# ---------------------------------------------------------------------------
# robots.txt
# ---------------------------------------------------------------------------

def test_robots_2xx_allows():
    robots = FakeResponse(200, "User-agent: *\nDisallow: /private/\n")
    record, session = fetch(routes_for(robots=robots), ART)
    assert record.fetch_status == "ok"
    assert session.urls == [ROBOTS, ART]


def test_robots_2xx_blocks_and_article_is_not_requested():
    robots = FakeResponse(200, "User-agent: *\nDisallow: /private/\n")
    url = "https://example.com/private/x"
    record, session = fetch(routes_for(url=url, robots=robots), url)
    assert record.fetch_status == "robots_blocked"
    assert url not in session.urls
    assert record.http_status is None and record.final_url is None
    assert "robots.txt" in record.error_detail


def test_robots_blocks_our_user_agent_specifically():
    robots = FakeResponse(200, "User-agent: AsimovWatch-ContentFetcher\nDisallow: /\n")
    record, session = fetch(routes_for(robots=robots), ART)
    assert record.fetch_status == "robots_blocked"
    assert ART not in session.urls


@pytest.mark.parametrize("code", [404, 410, 401, 403])
def test_robots_4xx_means_no_rules_and_allowed(code):
    record, session = fetch(routes_for(robots=FakeResponse(code)), ART)
    assert record.fetch_status == "ok"
    assert ART in session.urls


@pytest.mark.parametrize("robots", [
    FakeResponse(503),
    FakeResponse(500),
    requests.Timeout("lent"),
    requests.ConnectionError("sense xarxa"),
])
def test_robots_unreachable_does_not_download_the_article(robots):
    record, session = fetch(routes_for(robots=robots), ART)
    assert record.fetch_status == "robots_unreachable"
    assert ART not in session.urls
    assert record.http_status is None and record.text_content is None
    assert "robots.txt" in record.error_detail


def test_robots_redirect_to_a_blocking_host_blocks():
    first = "https://a.example/x"
    target = "https://b.example/y"
    routes = {
        "https://a.example/robots.txt": FakeResponse(404),
        first: FakeResponse(301, "", {"Location": target}),
        "https://b.example/robots.txt": FakeResponse(200, "User-agent: *\nDisallow: /\n"),
        target: ok_page(),
    }
    record, session = fetch(routes, first)
    assert record.fetch_status == "robots_blocked"
    assert "https://b.example/robots.txt" in session.urls  # robots.txt del desti
    assert target not in session.urls
    assert record.final_url == target
    assert record.http_status == 301


def test_robots_unreachable_on_redirect_target():
    first, target = "https://a.example/x", "https://b.example/y"
    routes = {
        "https://a.example/robots.txt": FakeResponse(404),
        first: FakeResponse(302, "", {"Location": target}),
        "https://b.example/robots.txt": FakeResponse(503),
        target: ok_page(),
    }
    record, session = fetch(routes, first)
    assert record.fetch_status == "robots_unreachable"
    assert target not in session.urls


def test_robots_is_cached_per_host_for_the_run():
    routes = routes_for()
    routes["https://example.com/b"] = ok_page()
    fetcher, session, _ = make_fetcher(routes)
    fetcher.fetch(ART)
    fetcher.fetch("https://example.com/b")
    assert session.urls.count(ROBOTS) == 1


def test_robots_unreachable_is_cached_too():
    routes = routes_for(robots=FakeResponse(503))
    routes["https://example.com/b"] = ok_page()
    fetcher, session, _ = make_fetcher(routes)
    first = fetcher.fetch(ART)
    second = fetcher.fetch("https://example.com/b")
    assert first.fetch_status == second.fetch_status == "robots_unreachable"
    assert session.urls.count(ROBOTS) == 1


def test_robots_uses_same_user_agent_and_bounded_timeout():
    record, session = fetch(routes_for(), ART, user_agent="Prova/9.9", robots_timeout=3.0, request_timeout=7.0)
    robots_call = session.calls_to(ROBOTS)[0]
    assert robots_call.headers == {"User-Agent": "Prova/9.9"}
    assert robots_call.timeout == 3.0
    assert robots_call.allow_redirects is False
    assert session.calls_to(ART)[0].timeout == 7.0


# ---------------------------------------------------------------------------
# Redireccions manuals
# ---------------------------------------------------------------------------

def test_redirects_are_followed_manually_and_final_url_is_the_last():
    routes = {
        "https://example.com/robots.txt": FakeResponse(404),
        ART: FakeResponse(301, "", {"Location": "/b"}),
        "https://example.com/b": FakeResponse(302, "", {"Location": "https://example.com/c#frag"}),
        "https://example.com/c": ok_page(),
    }
    record, session = fetch(routes, ART)
    assert record.fetch_status == "ok"
    assert record.final_url == "https://example.com/c"
    assert record.requested_url == ART
    assert all(call.allow_redirects is False for call in session.calls)


def test_too_many_redirects_is_error():
    routes = {"https://example.com/robots.txt": FakeResponse(404)}
    for i in range(7):
        routes[f"https://example.com/r{i}"] = FakeResponse(302, "", {"Location": f"/r{i + 1}"})
    record, session = fetch(routes, "https://example.com/r0", max_redirects=5)
    assert record.fetch_status == "error"
    assert "redireccions" in record.error_detail
    assert len([u for u in session.urls if u != ROBOTS]) == 6  # l'original + 5 redireccions


def test_redirect_without_location_is_http_error():
    record, _ = fetch(routes_for(response=FakeResponse(302)), ART)
    assert record.fetch_status == "http_error"
    assert record.http_status == 302


def test_redirect_to_non_http_url_is_error():
    record, _ = fetch(routes_for(response=FakeResponse(302, "", {"Location": "ftp://example.com/x"})), ART)
    assert record.fetch_status == "error"


# ---------------------------------------------------------------------------
# Repte anti-bot
# ---------------------------------------------------------------------------

CHALLENGE_PAGE = page(
    "FRA - Verification Required",
    "<p>FRA Website fra.europa.eu Due to unusually high traffic, we need to verify that requests are "
    "coming from real users. Please complete the verification below to continue.</p>",
)


def fra_routes(challenge_html=CHALLENGE_PAGE):
    challenge = "https://fra.europa.eu/challenge?return=%2Fen%2Fnews%2Fx"
    return {
        "https://fra.europa.eu/robots.txt": FakeResponse(404),
        "https://fra.europa.eu/en/news/x": FakeResponse(302, "", {"Location": challenge}),
        challenge: FakeResponse(200, challenge_html, HTML_HEADERS),
    }, challenge


def test_fra_verification_required_page_is_anti_bot_challenge():
    routes, challenge = fra_routes()
    record, _ = fetch(routes, "https://fra.europa.eu/en/news/x")
    assert record.fetch_status == "anti_bot_challenge"
    assert record.final_url == challenge
    assert record.http_status == 200
    assert record.text_content is None and record.content_hash is None and record.text_source is None


CHALLENGE_TITLES = [
    "Verification Required",
    "Just a moment...",
    "Attention Required! | Cloudflare",
    "Are you a robot?",
    "Comprovacio CAPTCHA",
]
SHORT_BODY = "<p>Please wait.</p>"


@pytest.mark.parametrize("title", CHALLENGE_TITLES)
def test_challenge_title_with_short_text_is_a_challenge(title):
    routes = routes_for(response=FakeResponse(200, page(title, SHORT_BODY), HTML_HEADERS))
    record, _ = fetch(routes, ART)
    assert record.fetch_status == "anti_bot_challenge"
    assert record.text_content is None and record.content_hash is None


@pytest.mark.parametrize("title", CHALLENGE_TITLES)
def test_challenge_title_with_long_text_is_not_a_challenge(title):
    routes = routes_for(response=FakeResponse(200, page(title, f"<article>{LONG_TEXT}</article>"), HTML_HEADERS))
    record, _ = fetch(routes, ART)
    assert record.fetch_status == "ok"
    assert record.text_source == "html_article"


def test_article_with_captcha_in_the_title_and_long_text_is_ok():
    title = "How CAPTCHA systems are changing"
    record, _ = fetch(routes_for(response=ok_page(title)), ART)
    assert record.fetch_status == "ok"


def test_just_a_moment_page_with_short_text_is_a_challenge():
    record, _ = fetch(routes_for(response=FakeResponse(200, page("Just a moment", SHORT_BODY), HTML_HEADERS)), ART)
    assert record.fetch_status == "anti_bot_challenge"


def test_title_rule_applies_strictly_below_the_short_text_threshold():
    body = "<p>" + "x" * 50 + "</p>"
    routes = routes_for(response=FakeResponse(200, page("Just a moment", body), HTML_HEADERS))
    below, _ = fetch(routes, ART, min_text_chars=51)   # 50 < 51: repte
    exact, _ = fetch(routes, ART, min_text_chars=50)   # 50 no es < 50: article
    assert below.fetch_status == "anti_bot_challenge"
    assert exact.fetch_status == "ok"


def test_empty_page_with_a_challenge_title_is_a_challenge():
    html = "<html><head><title>Just a moment...</title></head><body></body></html>"
    record, _ = fetch(routes_for(response=FakeResponse(200, html, HTML_HEADERS)), ART)
    assert record.fetch_status == "anti_bot_challenge"


@pytest.mark.parametrize("title,body,path", [
    ("Benvinguts", f"<p>{LONG_TEXT}</p>", "/challenge"),                  # camí, text llarg
    ("Benvinguts", f"<p>{LONG_TEXT}</p>", "/Challenge/"),                 # segment exacte, sense distingir majuscules
    ("Benvinguts", "<p>Please wait.</p>", "/challenge"),                  # camí, text curt
    ("Pagina", "<p>We need to verify that requests are coming from real users.</p>", "/article"),
    ("Pagina", f"<p>{LONG_TEXT} We need to verify that requests are coming from real users.</p>", "/article"),
])
def test_path_segment_and_body_phrase_always_apply(title, body, path):
    url = "https://example.org" + path
    routes = {
        "https://example.org/robots.txt": FakeResponse(404),
        url: FakeResponse(200, page(title, body), HTML_HEADERS),  # la URL demanada conserva la barra final
    }
    record, _ = fetch(routes, url)
    assert record.fetch_status == "anti_bot_challenge"


def test_is_anti_bot_challenge_unit():
    f = cf.is_anti_bot_challenge
    assert f("https://x.cat/challenge", "", "", 10_000, 400) is True
    assert f("https://x.cat/a", "", "please verify that requests are coming from real users", 10_000, 400) is True
    assert f("https://x.cat/a", "Just a moment", "", 399, 400) is True
    assert f("https://x.cat/a", "Just a moment", "", 400, 400) is False
    assert f("https://x.cat/a", "Just a moment", "", 0, 0) is False
    assert f("https://x.cat/challenges/a", "Challenges", "", 10_000, 400) is False


def test_real_article_with_challenges_in_the_url_is_not_a_challenge():
    url = ("https://www.edpb.europa.eu/news/"
           "edpb-publishes-statement-on-the-data-protection-challenges-of-smart-glasses_en")
    routes = {
        "https://www.edpb.europa.eu/robots.txt": FakeResponse(404),
        url: FakeResponse(200, page("EDPB statement on the data protection challenges of smart glasses",
                                    f"<article>{LONG_TEXT}</article>"), HTML_HEADERS),
    }
    record, _ = fetch(routes, url)
    assert record.fetch_status == "ok"
    assert record.text_source == "html_article"


@pytest.mark.parametrize("path", ["/challenges/smart-glasses", "/news/challenge-us", "/the-challenge", "/challenged"])
def test_challenge_word_inside_a_segment_is_not_a_segment_match(path):
    assert cf.has_challenge_path("https://example.org" + path) is False


@pytest.mark.parametrize("path", ["/challenge", "/a/challenge/b", "/Challenge", "/challenge/"])
def test_challenge_segment_is_a_match(path):
    assert cf.has_challenge_path("https://example.org" + path) is True


def test_challenge_is_neither_solved_nor_retried():
    routes, challenge = fra_routes()
    fetcher, session, _ = make_fetcher(routes)
    fetcher.fetch("https://fra.europa.eu/en/news/x")
    assert session.urls.count(challenge) == 1
    assert all(set(c.headers) <= {"User-Agent", "Accept"} for c in session.calls)


# ---------------------------------------------------------------------------
# User-Agent i capcaleres
# ---------------------------------------------------------------------------

def test_default_user_agent_and_headers_are_never_altered():
    assert DEFAULT_USER_AGENT == "AsimovWatch-ContentFetcher/1.0 (+https://asimovwatch.com)"
    resp = FakeResponse(403, "", HTML_HEADERS)  # com openai.com: cap canvi de capcalera per esquivar-ho
    record, session = fetch(routes_for(response=resp), ART)
    assert record.fetch_status == "access_denied"
    article_calls = session.calls_to(ART)
    assert len(article_calls) == 1
    assert article_calls[0].headers == {"User-Agent": record.user_agent, "Accept": ACCEPT_HEADER}
    assert session.calls_to(ROBOTS)[0].headers == {"User-Agent": record.user_agent}


def test_user_agent_is_stored_in_every_record():
    for response in (ok_page(), FakeResponse(404), requests.Timeout("x")):
        record, _ = fetch(routes_for(response=response), ART, user_agent="Altre/2.0")
        assert record.user_agent == "Altre/2.0"


def test_user_agent_from_environment():
    assert cf._user_agent_from_env({}) == DEFAULT_USER_AGENT
    assert cf._user_agent_from_env({"CONTENT_FETCH_USER_AGENT": "  X/1 (+https://x.cat) "}) == "X/1 (+https://x.cat)"
    assert cf._user_agent_from_env({"CONTENT_FETCH_USER_AGENT": "   "}) == DEFAULT_USER_AGENT


def test_min_text_chars_from_environment():
    assert cf._min_text_chars_from_env({}) == 400
    assert cf._min_text_chars_from_env({"CONTENT_FETCH_MIN_TEXT_CHARS": "250"}) == 250
    assert cf._min_text_chars_from_env({"CONTENT_FETCH_MIN_TEXT_CHARS": "x"}) == 400
    assert cf._min_text_chars_from_env({"CONTENT_FETCH_MIN_TEXT_CHARS": "-1"}) == 400


def test_other_user_agents_in_the_codebase_are_untouched():
    crawler = open(cf.__file__.replace("content_fetch.py", "crawler.py"), encoding="utf-8").read()
    candidates = open(cf.__file__.replace("content_fetch.py", "source_candidates.py"), encoding="utf-8").read()
    assert 'USER_AGENT = "AsimovWatch-EntriesCrawler/1.0 (+https://asimovwatch.com)"' in crawler
    assert 'user_agent = "Asimovwatch-FeedDetector/1.0"' in candidates


# ---------------------------------------------------------------------------
# Cadencia per host
# ---------------------------------------------------------------------------

def test_minimum_delay_per_host_but_not_across_hosts():
    routes = {
        "https://a.example/robots.txt": FakeResponse(404),
        "https://a.example/1": ok_page(),
        "https://a.example/2": ok_page(),
        "https://b.example/robots.txt": FakeResponse(404),
        "https://b.example/1": ok_page(),
    }
    fetcher, session, clock = make_fetcher(routes)
    fetcher.fetch("https://a.example/1")
    fetcher.fetch("https://a.example/2")
    fetcher.fetch("https://b.example/1")
    at = {(c.url): c.at for c in session.calls}
    assert at["https://a.example/1"] - at["https://a.example/robots.txt"] >= 2.0
    assert at["https://a.example/2"] - at["https://a.example/1"] >= 2.0
    # un altre host no espera: la seva primera peticio surt immediatament
    assert at["https://b.example/robots.txt"] == at["https://a.example/2"]
    assert at["https://b.example/1"] - at["https://b.example/robots.txt"] >= 2.0


# ---------------------------------------------------------------------------
# URL demanada a la xarxa: requested_url amb nomes els utm_* eliminats
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("https://example.com/a", "https://example.com/a"),
    ("https://example.com/a/", "https://example.com/a/"),                       # barra final intacta
    ("https://example.com/a?utm_source=x", "https://example.com/a"),
    ("https://example.com/a?b=2&utm_source=x&a=%20c&UTM_Medium=y&c", "https://example.com/a?b=2&a=%20c&c"),
    ("https://example.com/a?b=2&a=1", "https://example.com/a?b=2&a=1"),         # ordre intacte
    ("https://example.com/a?q=a+b&r=%C3%A0", "https://example.com/a?q=a+b&r=%C3%A0"),  # codificacio intacta
    ("https://example.com/a?x=1&&y=2&utm_a=1", "https://example.com/a?x=1&&y=2"),
    ("https://example.com/a?%75tm_x=1&k=v", "https://example.com/a?k=v"),       # clau percent-encoded
    ("https://example.com/a?xutm_a=1&utmx=2", "https://example.com/a?xutm_a=1&utmx=2"),  # no es prefix utm_
    ("https://example.com/a?utm_a=1#sec", "https://example.com/a#sec"),
    ("https://example.com/a?k=v#sec", "https://example.com/a?k=v#sec"),
    ("https://Example.COM/A/?utm_a=1&k=v", "https://Example.COM/A/?k=v"),       # host i majuscules intactes
    ("https://example.com/a?", "https://example.com/a?"),                       # sense utm_: tal com era
])
def test_strip_utm_params(raw, expected):
    assert strip_utm_params(raw) == expected


@pytest.mark.parametrize("raw", [None, 42, b"https://x.cat?utm_a=1", "", "no es una url?utm_a=1&b"])
def test_strip_utm_params_never_raises(raw):
    strip_utm_params(raw)


def test_network_url_keeps_everything_but_utm_and_normalized_url_is_separate():
    requested = "https://Example.com/A/?b=2&utm_source=x&a=%20c&UTM_medium=y&c"
    sent = "https://Example.com/A/?b=2&a=%20c&c"
    routes = {
        "https://example.com/robots.txt": FakeResponse(404),
        sent: ok_page(),
    }
    record, session = fetch(routes, requested)
    assert record.fetch_status == "ok"
    assert [u for u in session.urls if u != "https://example.com/robots.txt"] == [sent]
    assert record.requested_url == requested                      # tal com es va demanar
    assert record.normalized_url == normalize_url(requested)      # normalitzada, per a duplicats
    assert record.normalized_url == "https://example.com/A?a=+c&b=2&c="
    assert record.normalized_url != sent


def test_duplicate_lookup_uses_normalized_url_while_the_network_url_would_not_be_normalized():
    seen = []
    fetcher, session, _ = make_fetcher({})
    record = fetcher.fetch("https://Example.com/A/?utm_x=1&b=2&a=1",
                           find_reusable=lambda url: seen.append(url) or 3)
    assert seen == ["https://example.com/A?a=1&b=2"]
    assert record.fetch_status == "duplicate" and session.calls == []


def test_utm_is_not_stripped_from_redirect_targets_given_by_the_server():
    routes = {
        "https://example.com/robots.txt": FakeResponse(404),
        ART: FakeResponse(302, "", {"Location": "/b?utm_x=1&z=2"}),
        "https://example.com/b?utm_x=1&z=2": ok_page(),
    }
    record, _ = fetch(routes, ART)
    assert record.fetch_status == "ok"


# ---------------------------------------------------------------------------
# Content-Type
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("header,detail", [
    ("application/json", "unsupported_content_type:application/json"),
    ("image/png", "unsupported_content_type:image/png"),
    ("text/plain; charset=utf-8", "unsupported_content_type:text/plain"),
    ("Application/Octet-Stream", "unsupported_content_type:application/octet-stream"),
    ("text/xml", "unsupported_content_type:text/xml"),
])
def test_unsupported_content_type_is_an_error_without_text(header, detail):
    resp = FakeResponse(200, "<html><body>" + LONG_TEXT + "</body></html>", {"Content-Type": header})
    record, session = fetch(routes_for(response=resp), ART)
    assert record.fetch_status == "error"
    assert record.error_detail == detail
    assert record.content_type == header                  # es conserva la capcalera completa
    assert record.http_status == 200 and record.final_url == ART
    for name in ("text_content", "text_length", "content_hash", "text_source"):
        assert getattr(record, name) is None, name
    assert record.truncated is False
    assert resp.iterated is False                          # no es llegeix el cos


def test_missing_content_type_is_unsupported():
    resp = FakeResponse(200, "<html><body>" + LONG_TEXT + "</body></html>", {})
    record, _ = fetch(routes_for(response=resp), ART)
    assert record.fetch_status == "error"
    assert record.error_detail == "unsupported_content_type:missing"
    assert record.content_type is None


@pytest.mark.parametrize("header", [
    "text/html",
    "TEXT/HTML; charset=ISO-8859-1",
    "application/xhtml+xml",
    "application/xhtml+xml; charset=utf-8",
])
def test_html_and_xhtml_are_processed(header):
    resp = FakeResponse(200, page("T", f"<article>{LONG_TEXT}</article>"), {"Content-Type": header})
    record, _ = fetch(routes_for(response=resp), ART)
    assert record.fetch_status == "ok"


def test_pdf_is_still_pdf_unprocessed_and_not_unsupported():
    resp = FakeResponse(200, b"%PDF", {"Content-Type": "application/pdf"})
    record, _ = fetch(routes_for(response=resp), ART)
    assert record.fetch_status == "pdf_unprocessed" and record.error_detail is None


def test_content_type_is_only_checked_on_2xx():
    for code, status in ((403, "access_denied"), (404, "http_error")):
        record, _ = fetch(routes_for(response=FakeResponse(code, "x", {"Content-Type": "application/json"})), ART)
        assert record.fetch_status == status


# ---------------------------------------------------------------------------
# normalize_url
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("HTTPS://Example.COM/Path/", "https://example.com/Path"),            # esquema i host en minuscules; cami intacte
    ("https://example.com/a#frag", "https://example.com/a"),               # fragment fora
    ("https://example.com/a?utm_source=x&b=2&a=1", "https://example.com/a?a=1&b=2"),
    ("https://example.com/a?UTM_Medium=x&UTM_CAMPAIGN=y", "https://example.com/a"),
    ("https://example.com/a?b=2&a=2&a=1", "https://example.com/a?a=1&a=2&b=2"),  # ordenats (clau, valor)
    ("https://example.com/", "https://example.com"),
    ("https://example.com", "https://example.com"),
    ("https://example.com/a//", "https://example.com/a"),
    ("  https://example.com/a  ", "https://example.com/a"),
    ("https://example.com:8443/a/", "https://example.com:8443/a"),
    ("https://user:pw@Example.com/a", "https://user:pw@example.com/a"),
    ("http://[::1]:8080/A/", "http://[::1]:8080/A"),
    ("https://example.com/a?x=1&utm_x=2#z", "https://example.com/a?x=1"),
])
def test_normalize_url(raw, expected):
    assert normalize_url(raw) == expected


@pytest.mark.parametrize("raw", [
    "http://[::1",            # IPv6 malformada
    "https://example.com:abc/a",  # port invalid
    "https://example.com:99999/a",
    "/ruta/relativa",
    "example.com/a",
    "ftp://example.com/a",
    "mailto:a@b.cat",
    "http://",
    "",
    "   ",
    "no es una url",
    None,
    42,
    b"https://example.com",
])
def test_normalize_url_never_raises_and_returns_the_input(raw):
    assert normalize_url(raw) == raw


def test_normalize_url_is_idempotent():
    for raw in ("HTTPS://Example.COM/A/?b=2&utm_x=1&a=1#f", "https://example.com/", "http://[::1]:80/x/"):
        once = normalize_url(raw)
        assert normalize_url(once) == once


# ---------------------------------------------------------------------------
# validate_record
# ---------------------------------------------------------------------------

def valid_data(**overrides):
    data = dict(
        requested_url=ART, normalized_url=ART, fetch_status="ok", user_agent=DEFAULT_USER_AGENT,
        fetched_at=FIXED_NOW, truncated=False, final_url=ART, http_status=200,
        content_type="text/html", text_content="text", text_length=4,
        content_hash="a" * 64, text_source="html_body", error_detail=None, reused_from_fetch_id=None,
    )
    data.update(overrides)
    return data


def test_valid_record_passes_as_dict_and_as_dataclass():
    validate_record(valid_data())
    validate_record(FetchRecord(**valid_data()))
    validate_record(valid_data(final_url=None, http_status=None, text_content=None, text_length=None,
                               content_hash=None, text_source=None, content_type=None))


@pytest.mark.parametrize("overrides,fragment", [
    # NOT NULL
    (dict(requested_url=None), "requested_url es NOT NULL"),
    (dict(normalized_url=None), "normalized_url es NOT NULL"),
    (dict(user_agent=None), "user_agent es NOT NULL"),
    (dict(fetch_status=None), "fetch_status es NOT NULL"),
    (dict(fetched_at=None), "fetched_at es NOT NULL"),
    (dict(truncated=None), "truncated es NOT NULL"),
    # fetch_status
    (dict(fetch_status="acces_denegat"), "fetch_status"),
    (dict(fetch_status="text_massa_curt"), "fetch_status"),
    (dict(fetch_status="duplicat_de_12"), "fetch_status"),
    (dict(fetch_status="omes_per_decisio"), "fetch_status"),
    (dict(fetch_status=""), "fetch_status"),
    (dict(fetch_status="OK"), "fetch_status"),
    # text_source
    (dict(text_source="body"), "text_source"),
    (dict(text_source="main"), "text_source"),
    (dict(text_source="article"), "text_source"),
    (dict(text_source=""), "text_source"),
    (dict(text_source="html_pdf"), "text_source"),
    # content_hash
    (dict(content_hash="a" * 16), "content_hash"),
    (dict(content_hash="a" * 63), "content_hash"),
    (dict(content_hash="a" * 65), "content_hash"),
    (dict(content_hash="A" * 64), "content_hash"),
    (dict(content_hash="g" * 64), "content_hash"),
    (dict(content_hash=""), "content_hash"),
    (dict(content_hash="a" * 64 + "\n"), "content_hash"),
    (dict(content_hash=123), "content_hash"),
    # http_status
    (dict(http_status=99), "http_status"),
    (dict(http_status=600), "http_status"),
    (dict(http_status=0), "http_status"),
    (dict(http_status=""), "http_status"),
    (dict(http_status="200"), "http_status"),
    (dict(http_status=True), "http_status"),
    # text_length
    (dict(text_length=-1), "text_length"),
    (dict(text_length="4"), "text_length"),
    (dict(text_length=True), "text_length"),
    # reused_from_fetch_id
    (dict(reused_from_fetch_id="7"), "reused_from_fetch_id"),
    (dict(reused_from_fetch_id=True), "reused_from_fetch_id"),
    # tipus i buits
    (dict(truncated="false"), "truncated"),
    (dict(truncated=0), "truncated"),
    (dict(fetched_at="2026-10-26"), "fetched_at"),
    (dict(fetched_at=datetime(2026, 10, 26)), "zona horaria"),
    (dict(user_agent=""), "user_agent"),
    (dict(user_agent="   "), "user_agent"),
    (dict(requested_url=5), "requested_url"),
    (dict(final_url=""), "final_url"),
    (dict(content_type=""), "content_type"),
    (dict(text_content=""), "text_content"),
    (dict(error_detail=""), "error_detail"),
    (dict(error_detail=404), "error_detail"),
])
def test_validate_record_rejects(overrides, fragment):
    with pytest.raises(FetchRecordError) as excinfo:
        validate_record(valid_data(**overrides))
    assert fragment in str(excinfo.value)
    assert excinfo.value.violations


def test_validate_record_rejects_self_reuse_when_the_id_is_known():
    validate_record(valid_data(reused_from_fetch_id=5), record_id=6)
    with pytest.raises(FetchRecordError, match="mateixa fila"):
        validate_record(valid_data(reused_from_fetch_id=5), record_id=5)


def test_validate_record_reports_every_violation_and_unknown_fields():
    with pytest.raises(FetchRecordError) as excinfo:
        validate_record(valid_data(fetch_status="x", http_status=7, content_hash="z", extra=1))
    assert len(excinfo.value.violations) == 4
    assert FetchRecordError is not ValueError and isinstance(excinfo.value, ValueError)


def test_validate_record_flags_missing_not_null_keys_in_a_dict():
    data = valid_data()
    del data["user_agent"]
    with pytest.raises(FetchRecordError, match="user_agent es NOT NULL"):
        validate_record(data)


def test_empty_requested_and_normalized_url_are_allowed_only_as_not_null():
    validate_record(valid_data(requested_url="", normalized_url="", fetch_status="error"))


# ---------------------------------------------------------------------------
# Els 11 estats son assolibles
# ---------------------------------------------------------------------------

def test_all_eleven_statuses_are_reachable():
    produced = set()

    def run(routes, url=ART, **kwargs):
        record, _ = fetch(routes, url, **kwargs)
        produced.add(record.fetch_status)

    run(routes_for())                                                                   # ok
    run(routes_for(robots=FakeResponse(200, "User-agent: *\nDisallow: /\n")))          # robots_blocked
    run(routes_for(robots=FakeResponse(503)))                                           # robots_unreachable
    run(routes_for(response=FakeResponse(403)))                                         # access_denied
    routes, _ = fra_routes()
    run(routes, "https://fra.europa.eu/en/news/x")                                      # anti_bot_challenge
    run(routes_for(response=ok_page("T", "<p>curt</p>")))                               # text_too_short
    run(routes_for(response=requests.Timeout("x")))                                     # timeout
    run(routes_for(response=FakeResponse(500)))                                         # http_error
    run(routes_for(response=requests.ConnectionError("x")))                             # error
    run(routes_for(response=FakeResponse(200, b"%PDF", {"Content-Type": "application/pdf"})))  # pdf_unprocessed
    run(routes_for(), find_reusable=lambda url: 3)                                      # duplicate
    assert produced == FETCH_STATUSES
