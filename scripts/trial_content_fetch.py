#!/usr/bin/env python3
"""
Prova d'obtencio de contingut amb el MODUL REAL (app/content_fetch.py). SENSE BD.

ATENCIO: aquest script FA PETICIONS HTTP REALS. L'ha d'executar l'usuari.

Llegeix mostra_prova_obtencio.csv (columnes id, source_domain, canonical_url...) i
obte cada canonical_url amb ContentFetcher: User-Agent de CONTENT_FETCH_USER_AGENT
(o el per defecte), robots.txt, cadencia minima de 2,0 s per host, cap reintent i
cap capcalera extra. No es connecta a cap BD ni importa app.db. NO modifica el modul
ni la seva logica d'extraccio: nomes l'observa.

Una memoria de duplicats NOMES EN MEMORIA simula la reutilitzacio de produccio:
si una URL normalitzada ja ha donat un 'ok' en aquesta execucio, la repetida surt
com a 'duplicate' sense tornar-la a baixar.

Us:
    python scripts/trial_content_fetch.py prova_obtencio/mostra_prova_obtencio.csv
    python scripts/trial_content_fetch.py MOSTRA.csv --limit-per-domain 10 --skip-domains openai.com,example.org
    python scripts/trial_content_fetch.py MOSTRA.csv --ids 163,1249 --sample-report mostra.md --sample-ids 163,1249

Seleccio:
  - Per defecte: s'ometen els dominis d'--skip-domains (openai.com) i fra.europa.eu es
    limita a 5 entrades; --limit-per-domain N substitueix aquest limit per defecte i
    s'aplica a tots els dominis. Els omesos o fora de limit NO s'obtenen ni s'escriuen
    al CSV (nomes surten al resum).
  - --ids a,b,c: nomes aquestes entrades de la mostra (en l'ordre de la mostra). Es una
    seleccio explicita: NO s'hi apliquen --skip-domains ni --limit-per-domain. Els ids
    que no son a la mostra es reporten.

Informe de mostres (--sample-report FITXER.md --sample-ids a,b,c; van sempre juntes):
  per a cada entrada 'ok' indicada, escriu id, domini, URL, text_source, text_length i
  els PRIMERS 500 i els ULTIMS 500 caracters del text extret, sense modificar-lo (es el
  text_content que desaria el modul). Les entrades indicades que no son 'ok' o que no
  s'han processat surten a una seccio "No inclosos" amb el motiu.

Columnes del CSV (les 13 primeres no han canviat; les dues ultimes son NOMES del trial):
  id, source_domain, requested_url, normalized_url, fetch_status, http_status, final_url,
  text_source, text_length, content_hash, truncated, error_detail, durada_s,
  boilerplate_ratio, text_source_candidates

  boilerplate_ratio: de les frases del text (separades per salt de linia o punt; es
    compten frases DISTINTES i s'ignoren les buides), quina proporcio apareix tambe en
    almenys el 50 % de les ALTRES pagines 'ok' del MATEIX source_domain d'aquesta
    execucio (0.000-1.000, 3 decimals). Buida si el domini te menys de 3 pagines 'ok',
    si la fila no es 'ok' o si el text no te frases. Les pagines 'duplicate' no compten.
    El text desat pel modul ja te els espais normalitzats (sense salts de linia), de
    manera que a la pratica nomes separa el punt. Es calcula al final: durant
    l'execucio la columna es buida i el CSV es reescriu en acabar.
  text_source_candidates: que haurien donat els contenidors, sense canviar el que tria el
    modul (que tria <main>; si no n'hi ha, l'<article> amb mes text; si no, <body>). Format "article:<llargades>;main:<llargada>;body:<llargada>", on <llargades> son
    els caracters de CADA <article> separats per comes i 'none' si no n'hi ha; per
    exemple "article:5;main:none;body:2858". Les llargades es calculen amb la mateixa
    neteja que el modul. Buida si la pagina no s'ha analitzat (no 'ok' / 'text_too_short' /
    'anti_bot_challenge').
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import sys
import time
from collections import Counter, defaultdict
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

import requests
from bs4 import BeautifulSoup

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import content_fetch as cf  # noqa: E402
from app.content_fetch import ContentFetcher, FetchRecordError, validate_record  # noqa: E402

DEFAULT_SKIP_DOMAINS = "openai.com"
FRA_DOMAIN = "fra.europa.eu"
FRA_DEFAULT_LIMIT = 5
DEFAULT_OUTPUT = "resultat_trial_content_fetch.csv"
SAMPLE_CHARS = 500
BOILERPLATE_MIN_PAGES = 3
BOILERPLATE_SHARE = 0.5

CSV_FIELDS = [
    "id", "source_domain", "requested_url", "normalized_url", "fetch_status", "http_status",
    "final_url", "text_source", "text_length", "content_hash", "truncated", "error_detail", "durada_s",
    "boilerplate_ratio", "text_source_candidates",
]
REQUIRED_INPUT_COLUMNS = ("id", "source_domain", "canonical_url")
PARSED_STATUSES = frozenset({"ok", "text_too_short", "anti_bot_challenge"})


# ---------------------------------------------------------------------------
# Arguments i seleccio
# ---------------------------------------------------------------------------

def parse_id_list(value: Optional[str]) -> List[str]:
    """'163, 1249,163' -> ['163', '1249'] (sense buits ni repetits, en ordre)."""
    seen: List[str] = []
    for item in (value or "").split(","):
        item = item.strip()
        if item and item not in seen:
            seen.append(item)
    return seen


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Prova d'obtencio de contingut (sense BD; fa xarxa real).")
    parser.add_argument("sample_csv", help="Ruta de mostra_prova_obtencio.csv")
    parser.add_argument("--output", default=DEFAULT_OUTPUT, help=f"CSV de sortida (per defecte {DEFAULT_OUTPUT})")
    parser.add_argument("--overwrite", action="store_true", help="Permet sobreescriure el CSV i l'informe de sortida")
    parser.add_argument(
        "--skip-domains", default=DEFAULT_SKIP_DOMAINS,
        help=f"Dominis a no obtenir, separats per comes (per defecte {DEFAULT_SKIP_DOMAINS}; buit = cap)",
    )
    parser.add_argument(
        "--limit-per-domain", type=int, default=None, metavar="N",
        help=f"Maxim d'entrades per domini. Sense aquest argument: sense limit, excepte {FRA_DOMAIN} (5).",
    )
    parser.add_argument(
        "--ids", default=None, metavar="ID,ID,...",
        help="Processa NOMES aquestes entrades de la mostra (no s'hi apliquen --skip-domains ni --limit-per-domain).",
    )
    parser.add_argument("--sample-report", default=None, metavar="FITXER.md",
                        help="Escriu un informe Markdown amb el principi i el final del text de les entrades de --sample-ids")
    parser.add_argument("--sample-ids", default=None, metavar="ID,ID,...",
                        help="Entrades 'ok' de l'informe (requereix --sample-report)")
    args = parser.parse_args(argv)
    if args.limit_per_domain is not None and args.limit_per_domain < 0:
        parser.error("--limit-per-domain no pot ser negatiu")
    if bool(args.sample_report) != bool(args.sample_ids):
        parser.error("--sample-report i --sample-ids van sempre juntes")
    if args.ids is not None and not parse_id_list(args.ids):
        parser.error("--ids no conte cap id")
    if args.sample_ids is not None and not parse_id_list(args.sample_ids):
        parser.error("--sample-ids no conte cap id")
    return args


def read_sample(path: str) -> List[Dict[str, str]]:
    with open(path, newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        missing = [c for c in REQUIRED_INPUT_COLUMNS if c not in (reader.fieldnames or [])]
        if missing:
            raise SystemExit(f"Al CSV d'entrada falten les columnes: {', '.join(missing)}")
        return list(reader)


def _domain(value: str) -> str:
    return (value or "").strip().lower()


def parse_skip_domains(value: str) -> List[str]:
    return [d for d in (_domain(x) for x in (value or "").split(",")) if d]


def is_skipped(domain: str, skip_domains: Sequence[str]) -> bool:
    return any(domain == s or domain.endswith("." + s) for s in skip_domains)


def select_rows(
    rows: Sequence[Dict[str, str]],
    skip_domains: Sequence[str],
    limit_per_domain: Optional[int],
) -> Tuple[List[Dict[str, str]], Counter, Counter]:
    """Retorna (files a obtenir, omesos per domini, fora de limit per domini). Ordre del CSV."""
    selected: List[Dict[str, str]] = []
    skipped: Counter = Counter()
    over_limit: Counter = Counter()
    taken: Counter = Counter()
    for row in rows:
        domain = _domain(row.get("source_domain", ""))
        if is_skipped(domain, skip_domains):
            skipped[domain] += 1
            continue
        limit = limit_per_domain
        if limit is None and domain == FRA_DOMAIN:
            limit = FRA_DEFAULT_LIMIT
        if limit is not None and taken[domain] >= limit:
            over_limit[domain] += 1
            continue
        taken[domain] += 1
        selected.append(row)
    return selected, skipped, over_limit


def select_by_ids(
    rows: Sequence[Dict[str, str]], ids: Sequence[str]
) -> Tuple[List[Dict[str, str]], List[str]]:
    """Files amb aquests ids (en l'ordre de la mostra) i ids que no hi son. Sense skip ni limit."""
    wanted = set(ids)
    selected = [row for row in rows if (row.get("id") or "").strip() in wanted]
    found = {(row.get("id") or "").strip() for row in selected}
    return selected, [i for i in ids if i not in found]


# ---------------------------------------------------------------------------
# Observacio del mòdul (sense modificar-lo)
# ---------------------------------------------------------------------------

class _RecordingResponse:
    """Resposta que copia (tee) els bytes que el modul llegeix; tota la resta es delega tal qual."""

    def __init__(self, response: Any):
        self._response = response
        self._chunks: List[bytes] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self._response, name)

    def iter_content(self, *args: Any, **kwargs: Any):
        for chunk in self._response.iter_content(*args, **kwargs):
            self._chunks.append(chunk)
            yield chunk

    @property
    def captured(self) -> bytes:
        return b"".join(self._chunks)


class RecordingSession:
    """Embolcall passiu d'una sessio HTTP: no canvia peticions, capcaleres ni respostes."""

    def __init__(self, session: Any):
        self._session = session
        self._responses: Dict[str, _RecordingResponse] = {}

    def get(self, url: str, *args: Any, **kwargs: Any) -> Any:
        wrapped = _RecordingResponse(self._session.get(url, *args, **kwargs))
        self._responses[url] = wrapped
        return wrapped

    def take(self, url: Optional[str]) -> Optional[_RecordingResponse]:
        return self._responses.get(url) if url else None

    def clear(self) -> None:
        self._responses.clear()


def analyze_candidates(html: str) -> Dict[str, Any]:
    """
    Llargada (en caracters) que donaria cada contenidor. Replica la NETEJA de cf.extract_page
    (mateixes etiquetes eliminades, mateixa clau de neteja) sense triar-ne cap. El modul tria
    <main> si n'hi ha; si no, l'<article> amb mes text; si no, <body> (no el mes llarg); un test
    comprova que aplicar aquesta regla a aquests valors dona el contenidor i la llargada del modul.
    """
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    for tag in soup(["nav", "header", "footer", "aside", "form", "svg", "iframe"]):
        tag.decompose()
    articles = [len(cf.clean_text(a.get_text(" "))) for a in soup.find_all("article")]
    main = soup.find("main")
    body = soup.body or soup
    return {
        "article": articles,
        "main": len(cf.clean_text(main.get_text(" "))) if main else None,
        "body": len(cf.clean_text(body.get_text(" "))),
    }


def format_candidates(info: Dict[str, Any]) -> str:
    article = ",".join(str(n) for n in info["article"]) if info["article"] else "none"
    main = "none" if info["main"] is None else str(info["main"])
    return f"article:{article};main:{main};body:{info['body']}"


def candidates_for(record: Any, session: RecordingSession, max_bytes: int) -> str:
    """Candidats del que ha parsejat el modul per a aquest registre; buit si no s'ha parsejat."""
    if record.fetch_status not in PARSED_STATUSES:
        return ""
    captured = session.take(record.final_url)
    if captured is None:
        return ""
    try:
        html = cf._decode(captured.captured[:max_bytes], getattr(captured, "encoding", None))
        return format_candidates(analyze_candidates(html))
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Boilerplate
# ---------------------------------------------------------------------------

_SENTENCE_SPLIT_RE = re.compile(r"[.\r\n]+")


def split_sentences(text: Optional[str]) -> List[str]:
    """Frases separades per punt o salt de linia; sense espais als extrems ni frases buides."""
    return [part.strip() for part in _SENTENCE_SPLIT_RE.split(text or "") if part.strip()]


def compute_boilerplate_ratios(
    pages: Sequence[Tuple[Any, str, Set[str]]]
) -> Dict[Any, float]:
    """
    pages: (clau, domini, frases distintes) de les pagines 'ok'. Retorna {clau: proporcio}.
    Una frase compta com a boilerplate si apareix en >= 50 % de les ALTRES pagines 'ok' del
    domini. Cap resultat per als dominis amb menys de 3 pagines 'ok' ni per a pagines sense frases.
    """
    by_domain: Dict[str, List[Tuple[Any, Set[str]]]] = defaultdict(list)
    for key, domain, sentences in pages:
        by_domain[domain].append((key, sentences))

    ratios: Dict[Any, float] = {}
    for items in by_domain.values():
        total = len(items)
        if total < BOILERPLATE_MIN_PAGES:
            continue
        in_pages = Counter(sentence for _, sentences in items for sentence in sentences)
        others = total - 1
        for key, sentences in items:
            if not sentences:
                continue
            # (pagines que la contenen - aquesta) / altres >= 0.5, sense decimals
            shared = sum(1 for s in sentences if (in_pages[s] - 1) * 2 >= others)
            ratios[key] = shared / len(sentences)
    return ratios


# ---------------------------------------------------------------------------
# Sortides
# ---------------------------------------------------------------------------

def record_to_row(entry_id: str, domain: str, record: Any, duration: float, candidates: str = "") -> Dict[str, str]:
    """Fila del CSV: els camps sense valor queden buits. boilerplate_ratio s'omple al final."""
    def cell(value: Any) -> str:
        return "" if value is None else str(value)

    return {
        "id": entry_id,
        "source_domain": domain,
        "requested_url": cell(record.requested_url),
        "normalized_url": cell(record.normalized_url),
        "fetch_status": record.fetch_status,
        "http_status": cell(record.http_status),
        "final_url": cell(record.final_url),
        "text_source": cell(record.text_source),
        "text_length": cell(record.text_length),
        "content_hash": cell(record.content_hash),
        "truncated": "true" if record.truncated else "false",
        "error_detail": cell(record.error_detail),
        "durada_s": f"{duration:.2f}",
        "boilerplate_ratio": "",
        "text_source_candidates": candidates,
    }


def _fence_for(text: str) -> str:
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    return "`" * max(3, longest + 1)


def _code_block(text: str) -> List[str]:
    fence = _fence_for(text)
    return [fence, text, fence]


def build_sample_report(
    sample_ids: Sequence[str],
    ok_entries: Dict[str, Dict[str, Any]],
    not_included: Dict[str, str],
) -> str:
    """Markdown amb el principi i el final del text de les entrades 'ok' de la mostra."""
    lines = [
        "# Mostra del text extret",
        "",
        "Generat per `scripts/trial_content_fetch.py`. El text es el `text_content` que desaria el modul, "
        f"sense modificar; es mostren els primers {SAMPLE_CHARS} i els ultims {SAMPLE_CHARS} caracters.",
        "",
    ]
    for entry_id in sample_ids:
        info = ok_entries.get(entry_id)
        if info is None:
            continue
        text = info["text"]
        lines += [
            f"## id {entry_id} - {info['domain']}",
            "",
            f"- id: {entry_id}",
            f"- domini: {info['domain']}",
            f"- URL: {info['url']}",
            f"- text_source: {info['text_source']}",
            f"- text_length: {info['text_length']}",
        ]
        if len(text) <= 2 * SAMPLE_CHARS:
            lines.append(f"- Nota: el text te {len(text)} caracters (<= {2 * SAMPLE_CHARS}); els dos blocs se solapen.")
        lines += ["", f"### Primers {SAMPLE_CHARS} caracters", ""]
        lines += _code_block(text[:SAMPLE_CHARS])
        lines += ["", f"### Ultims {SAMPLE_CHARS} caracters", ""]
        lines += _code_block(text[-SAMPLE_CHARS:])
        lines.append("")
    if not_included:
        lines += ["## No inclosos", ""]
        lines += [f"- id {entry_id}: {reason}" for entry_id, reason in not_included.items()]
        lines.append("")
    return "\n".join(lines)


def summarize(
    rows: Sequence[Dict[str, str]], skipped: Counter, over_limit: Counter, violations: int,
    missing_ids: Sequence[str] = (),
) -> List[str]:
    by_status = Counter(r["fetch_status"] for r in rows)
    by_domain: Dict[str, Counter] = defaultdict(Counter)
    for r in rows:
        by_domain[r["source_domain"]][r["fetch_status"]] += 1

    lines = ["", "=== RESUM ===", f"Entrades obtingudes: {len(rows)}"]
    if skipped:
        lines.append("Omeses per --skip-domains: " + ", ".join(f"{d}={n}" for d, n in sorted(skipped.items())))
    if over_limit:
        lines.append("Fora del limit per domini: " + ", ".join(f"{d}={n}" for d, n in sorted(over_limit.items())))
    if missing_ids:
        lines.append("Ids de --ids que no son a la mostra: " + ", ".join(missing_ids))
    if violations:
        lines.append(f"ATENCIO: {violations} registres no compleixen el contracte de la BD (validate_record)")
    lines += ["", "Per fetch_status:"]
    lines += [f"  {status:<20} {count:>5}" for status, count in sorted(by_status.items(), key=lambda kv: (-kv[1], kv[0]))]
    lines += ["", "Per domini:"]
    for domain, counter in sorted(by_domain.items(), key=lambda kv: (-sum(kv[1].values()), kv[0])):
        detail = ", ".join(f"{s}={n}" for s, n in sorted(counter.items(), key=lambda kv: (-kv[1], kv[0])))
        lines.append(f"  {domain:<28} {sum(counter.values()):>4}  {detail}")
    return lines


def _write_csv(path: str, rows: Sequence[Dict[str, str]]) -> None:
    """Reescriu el CSV complet de manera atomica (fitxer temporal + os.replace)."""
    tmp = f"{path}.tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    os.replace(tmp, path)


def main(
    argv: Optional[Sequence[str]] = None,
    session: Any = None,
    fetcher_options: Optional[Dict[str, Any]] = None,
    out: Any = None,
    err: Any = None,
) -> int:
    """`session` i `fetcher_options` (sleep, clock, now, min_text_chars...) permeten simular-ho als tests."""
    out = out or sys.stdout
    err = err or sys.stderr
    args = parse_args(argv)

    for label, path in (("CSV", args.output), ("informe", args.sample_report)):
        if path and os.path.exists(path) and not args.overwrite:
            print(f"{path} ({label}) ja existeix; usa --overwrite o un altre nom.", file=err)
            return 2

    rows = read_sample(args.sample_csv)
    skipped: Counter = Counter()
    over_limit: Counter = Counter()
    missing_ids: List[str] = []
    if args.ids is not None:
        selected, missing_ids = select_by_ids(rows, parse_id_list(args.ids))
        if missing_ids:
            print("Ids que no son a la mostra: " + ", ".join(missing_ids), file=err)
        if not selected:
            print("Cap entrada seleccionada amb --ids.", file=err)
            return 2
    else:
        selected, skipped, over_limit = select_rows(rows, parse_skip_domains(args.skip_domains), args.limit_per_domain)

    recorder = RecordingSession(session if session is not None else requests.Session())
    fetcher = ContentFetcher(recorder, **(fetcher_options or {}))
    sample_ids = parse_id_list(args.sample_ids)

    print(f"ATENCIO: peticions HTTP reals. User-Agent: {fetcher.user_agent}", file=err)
    print(f"Entrades a obtenir: {len(selected)} de {len(rows)}. Cadencia minima {fetcher.delay_per_host}s per host.", file=err)

    ok_urls: Dict[str, int] = {}                      # memoria de duplicats NOMES en memoria (id simulat)
    written: List[Dict[str, str]] = []
    ok_pages: List[Tuple[int, str, Set[str]]] = []     # (index a `written`, domini, frases distintes)
    ok_samples: Dict[str, Dict[str, Any]] = {}
    status_by_id: Dict[str, str] = {}
    violations = 0
    interrupted = False

    with open(args.output, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        writer.writeheader()
        try:
            for index, row in enumerate(selected, 1):
                recorder.clear()
                started = time.monotonic()
                record = fetcher.fetch_entry_content(
                    row.get("canonical_url"),
                    find_reusable=lambda url: ok_urls.get(url),
                )
                duration = time.monotonic() - started
                try:
                    validate_record(record)
                except FetchRecordError as exc:
                    violations += 1
                    print(f"  CONTRACTE: id={row.get('id')}: {exc}", file=err)
                if record.fetch_status == "ok" and record.normalized_url not in ok_urls:
                    ok_urls[record.normalized_url] = index

                entry_id = (row.get("id") or "").strip()
                domain = _domain(row.get("source_domain", ""))
                candidates = candidates_for(record, recorder, fetcher.max_bytes)
                out_row = record_to_row(entry_id, domain, record, duration, candidates)
                writer.writerow(out_row)
                handle.flush()
                written.append(out_row)
                status_by_id[entry_id] = record.fetch_status

                if record.fetch_status == "ok":
                    ok_pages.append((len(written) - 1, domain, set(split_sentences(record.text_content))))
                    if entry_id in sample_ids:
                        ok_samples[entry_id] = {
                            "domain": domain, "url": record.requested_url, "text_source": record.text_source,
                            "text_length": record.text_length, "text": record.text_content or "",
                        }
                print(f"[{index}/{len(selected)}] id={entry_id} {domain} "
                      f"{out_row['fetch_status']} {out_row['durada_s']}s", file=err)
        except KeyboardInterrupt:
            interrupted = True
            print("Interromput: es desa el que hi ha.", file=err)
    recorder.clear()

    # boilerplate_ratio: necessita totes les pagines 'ok' del domini; es reescriu el CSV al final
    for key, ratio in compute_boilerplate_ratios(ok_pages).items():
        written[key]["boilerplate_ratio"] = f"{ratio:.3f}"
    _write_csv(args.output, written)

    if args.sample_report:
        not_included = {}
        for entry_id in sample_ids:
            if entry_id in ok_samples:
                continue
            status = status_by_id.get(entry_id)
            not_included[entry_id] = (
                "no processada en aquesta execucio" if status is None else f"fetch_status={status} (no es 'ok')"
            )
        with open(args.sample_report, "w", encoding="utf-8") as handle:
            handle.write(build_sample_report(sample_ids, ok_samples, not_included))

    for line in summarize(written, skipped, over_limit, violations, missing_ids):
        print(line, file=out)
    print(f"\nCSV: {args.output}", file=out)
    if args.sample_report:
        print(f"Informe de mostres: {args.sample_report}", file=out)
    return 130 if interrupted else 0


if __name__ == "__main__":
    sys.exit(main())
