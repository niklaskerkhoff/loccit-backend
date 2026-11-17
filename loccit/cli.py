"""Command line entry point.

`python -m loccit [thesis.pdf --bib refs.bib]` starts the server,
`python -m loccit resolve thesis.pdf --bib refs.bib` prints the mapping,
`python -m loccit set-key` stores the Anthropic API key in the keychain,
`python -m loccit check-models` compares models.toml with the API.
"""

import argparse
import getpass
import logging
import socket
import sys
import threading
import webbrowser
from pathlib import Path

import anthropic
import uvicorn

from loccit import models, paths
from loccit import resolve as res
from loccit.api import DEFAULT_PORT, DEV_SERVER_PORT, create_app
from loccit.bibfile import load_bib
from loccit.citations import CitationScan, scan_citations
from loccit.settings import load_settings, save_settings
from loccit.workspace import InvalidPdf, Workspace
from loccit.zotero import (
    KEY_FROM_EXPORT,
    LibraryIndex,
    ZoteroApiDisabled,
    ZoteroClient,
    ZoteroError,
    ZoteroItem,
    ZoteroNotRunning,
)

ZOTERO_NOT_RUNNING = (
    "Zotero antwortet nicht auf http://localhost:23119. Bitte Zotero starten und erneut versuchen."
)
ZOTERO_API_DISABLED = (
    "Zotero lehnt den Zugriff ab (HTTP 403). Bitte in Zotero unter Einstellungen → Erweitert "
    "„Anderen Anwendungen auf diesem Computer die Kommunikation mit Zotero erlauben“ einschalten."
)

_WAY_A_REASONS = {
    "no_bib": "keine .bib",
    "not_in_bib": "nicht in .bib",
    "duplicate_in_bib": "doppelt in .bib",
    "no_hit": "kein Treffer",
    "contradicts_bib": "Treffer widersprechen der .bib",
}
_WAY_LABELS = {
    "doi": "DOI",
    "url": "URL",
    "title+year": "Titel+Jahr",
    "suffix": "Grundschlüssel",
    KEY_FROM_EXPORT: "",
}
_FIELD_LABELS = {"doi": "DOI", "url": "URL", "type": "Eintragstyp"}


DEV_SERVER_URL = f"http://localhost:{DEV_SERVER_PORT}/"


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["resolve"]:
        parser = argparse.ArgumentParser(
            prog="python -m loccit resolve",
            description="Show how every citation of a thesis PDF maps to a Zotero PDF.",
        )
        parser.add_argument("pdf", type=Path, help="PDF of the thesis")
        parser.add_argument("--bib", type=Path, help=".bib file of the thesis")
        args = parser.parse_args(argv[1:])
        return run_resolve(args.pdf, args.bib)
    if argv[:1] == ["set-key"]:
        parser = argparse.ArgumentParser(
            prog="python -m loccit set-key",
            description="Store the Anthropic API key in the system keychain.",
        )
        parser.add_argument("--delete", action="store_true", help="remove the stored key")
        return run_set_key(parser.parse_args(argv[1:]).delete)
    if argv[:1] == ["check-models"]:
        parser = argparse.ArgumentParser(
            prog="python -m loccit check-models",
            description="Compare ~/.loccit/models.toml with what the API reports.",
        )
        parser.parse_args(argv[1:])
        return run_check_models()

    parser = argparse.ArgumentParser(
        prog="python -m loccit",
        description="Start loccit and open it in the browser.",
        epilog="Other commands: resolve thesis.pdf [--bib refs.bib], set-key, check-models",
    )
    parser.add_argument("pdf", nargs="?", type=Path, help="PDF of the thesis to open")
    parser.add_argument("--bib", type=Path, help=".bib file of the thesis")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "--frontend",
        type=Path,
        metavar="DIST",
        help="folder of the built web frontend; remembered in ~/.loccit/settings.toml",
    )
    parser.add_argument("--no-browser", action="store_true", help="do not open a browser tab")
    parser.add_argument(
        "--debug",
        action="store_true",
        help="log details, e.g. which paper PDFs MuPDF found damaged",
    )
    args = parser.parse_args(argv)
    if args.bib and not args.pdf:
        parser.error("--bib needs the PDF of the thesis")
    return run_server(args)


def run_server(args: argparse.Namespace) -> int:
    # --debug shows loccit's own details only; libraries stay at WARNING.
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("loccit").setLevel(logging.DEBUG if args.debug else logging.WARNING)
    root = paths.data_dir()
    settings = load_settings(paths.settings_file(root))
    if args.frontend:
        dist = args.frontend.expanduser().resolve()
        if not (dist / "index.html").is_file():
            print(
                f"Kein gebautes Frontend in {dist} (index.html fehlt). "
                "Erst im Frontend-Repo `npm run build` ausführen.",
                file=sys.stderr,
            )
            return 2
        settings.frontend_dist = dist
        save_settings(paths.settings_file(root), settings)

    try:
        # Writes models.toml with the defaults on the first start.
        models.load_model_settings(paths.models_file(root))
    except models.ModelSettingsError as error:
        print(f"models.toml ist fehlerhaft: {error}", file=sys.stderr)
        return 2
    workspace = Workspace(root)
    fragment = ""
    if args.pdf:
        for path in (args.pdf, args.bib):
            if path and not path.is_file():
                print(f"Datei nicht gefunden: {path}", file=sys.stderr)
                return 2
        try:
            document = workspace.import_document(
                args.pdf.name, args.pdf.read_bytes(), args.bib.read_bytes() if args.bib else None
            )
        except InvalidPdf:
            print(f"Keine lesbare PDF-Datei: {args.pdf}", file=sys.stderr)
            return 2
        fragment = f"#/doc/{document.id}"

    if _port_in_use(args.port):
        print(
            f"Port {args.port} ist belegt. Läuft loccit schon? Sonst mit --port einen anderen wählen.",
            file=sys.stderr,
        )
        return 2

    frontend = settings.frontend_dist
    backend_url = f"http://127.0.0.1:{args.port}/"
    if frontend and (frontend / "index.html").is_file():
        print(f"loccit läuft auf {backend_url}{fragment} (Beenden mit Strg+C)", flush=True)
        if not args.no_browser:
            threading.Timer(0.7, webbrowser.open, [backend_url + fragment]).start()
    else:
        if frontend:
            print(f"Hinterlegtes Frontend nicht gefunden: {frontend}")
        frontend = None
        print(
            f"loccit-Backend läuft auf {backend_url} (Beenden mit Strg+C).\n"
            "Es ist kein gebautes Frontend hinterlegt. Zwei Möglichkeiten, im Frontend-Repo:\n"
            f"  Entwicklung: `npm run dev` starten und {DEV_SERVER_URL}{fragment} öffnen\n"
            "  Dauerhaft:   `npm run build`, dann einmalig\n"
            "               python -m loccit --frontend /pfad/zu/loccit-web-frontend/dist",
            flush=True,
        )
    uvicorn.run(
        create_app(workspace, frontend, port=args.port),
        host="127.0.0.1",
        port=args.port,
        log_level="warning",
    )
    return 0


def run_set_key(delete: bool) -> int:
    if delete:
        removed = models.delete_api_key()
        print(
            "API-Key aus dem Schlüsselbund entfernt." if removed else "Es war kein Key gespeichert."
        )
        return 0
    # getpass keeps the key out of the terminal scrollback and shell history.
    key = getpass.getpass("Anthropic API-Key (Eingabe bleibt unsichtbar): ").strip()
    if not key:
        print("Kein Key eingegeben, nichts gespeichert.", file=sys.stderr)
        return 2
    models.set_api_key(key)
    print("API-Key im System-Schlüsselbund gespeichert (Dienst „loccit“).")
    return 0


def run_check_models(client: anthropic.Anthropic | None = None) -> int:
    """Ask the Models API what each configured model accepts and report
    where models.toml offers something else."""
    try:
        settings = models.load_model_settings(paths.models_file())
    except models.ModelSettingsError as error:
        print(f"models.toml ist fehlerhaft: {error}", file=sys.stderr)
        return 2
    if client is None:
        key = models.get_api_key()
        if not key:
            print(
                "Kein API-Key: ANTHROPIC_API_KEY setzen oder `python -m loccit set-key`.",
                file=sys.stderr,
            )
            return 2
        client = anthropic.Anthropic(api_key=key)

    problems = 0
    for model in settings.models:
        try:
            info = client.models.retrieve(model.id)
        except anthropic.NotFoundError:
            print(f"✗ {model.id}: Die API kennt dieses Modell nicht.")
            problems += 1
            continue
        except anthropic.APIError as error:
            print(f"Abfrage fehlgeschlagen: {error}", file=sys.stderr)
            return 2
        capabilities = info.capabilities.model_dump() if info.capabilities else {}
        notes = []
        if not _supported(capabilities, ("citations",)):
            notes.append("Citations werden laut API nicht unterstützt")
        for level in model.levels:
            if level == models.OFF:
                path = ("thinking", "types", model.thinking_off)
            else:
                path = ("effort", level)
            if not _supported(capabilities, path):
                notes.append(f"Stufe „{level}“ wird laut API nicht unterstützt")
        if models.OFF not in model.levels:
            for off in ("disabled", "between_tools"):
                if _supported(capabilities, ("thinking", "types", off)):
                    notes.append(f'„off“ wäre möglich (thinking_off = "{off}")')
        problems += len(notes)
        print(f"{'✗' if notes else '✓'} {model.id}: {'; '.join(notes) or 'passt zur API'}")
    return 1 if problems else 0


def _supported(capabilities: dict, path: tuple[str, ...]) -> bool:
    node = capabilities
    for name in path:
        node = (node or {}).get(name) if isinstance(node, dict) else None
    return bool(isinstance(node, dict) and node.get("supported"))


def _port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return True
    return False


def run_resolve(pdf: Path, bib_path: Path | None) -> int:
    for path in (pdf, bib_path):
        if path and not path.is_file():
            print(f"Datei nicht gefunden: {path}", file=sys.stderr)
            return 2

    scan = scan_citations(pdf)
    if not scan.groups:
        print(no_citation_links(scan), file=sys.stderr)
        return 1

    client = ZoteroClient()
    try:
        library = client.load_library()
        resolver = res.Resolver(
            library,
            client.attachment_file_url,
            bib=load_bib(bib_path) if bib_path else None,
            overrides=res.load_overrides(paths.overrides_file()),
        )
        resolutions = [resolver.resolve(key) for key in scan.keys]
    except ZoteroNotRunning:
        print(ZOTERO_NOT_RUNNING, file=sys.stderr)
        return 2
    except ZoteroApiDisabled:
        print(ZOTERO_API_DISABLED, file=sys.stderr)
        return 2
    except ZoteroError as error:
        print(f"Zotero-Fehler: {error}", file=sys.stderr)
        return 2
    finally:
        client.close()

    print(_report(scan, library, resolutions, has_bib=resolver.bib is not None))
    return 0


def no_citation_links(scan: CitationScan) -> str:
    message = (
        "Das PDF enthält keine Zitat-Links (Link-Ziele der Form cite.<key> oder "
        "cite.<refsection>@<key>). Ist es mit hyperref gebaut?"
    )
    if scan.dest_prefixes:
        found = ", ".join(f"{prefix}.* ({n})" for prefix, n in scan.dest_prefixes.most_common(8))
        message += f"\nGefundene benannte Link-Ziele: {found}"
    else:
        message += f"\nLinks insgesamt: {scan.link_count}, keiner mit benanntem Ziel."
    return message


def _report(
    scan: CitationScan,
    library: LibraryIndex,
    resolutions: list[res.Resolution],
    has_bib: bool,
) -> str:
    citation_count = sum(len(group.citations) for group in scan.groups)
    pdf_count = sum(len(item.pdfs) for item in library.items.values())
    out = [
        f"Arbeit: {citation_count} Zitate an {len(scan.groups)} Stellen, "
        f"{len(resolutions)} verschiedene Schlüssel.",
        f"Zotero: {len(library.items)} Einträge, {pdf_count} PDF-Anhänge.",
        "",
        _table(
            ["Schlüssel", "Weg A (.bib)", "Weg B (Schlüssel)", "A=B", "PDF oder Grund"],
            [
                [r.cite_key, _way(r.way_a), _way(r.way_b), _agreement(r), _outcome(r)]
                for r in resolutions
            ],
        ),
        "",
    ]

    multi = [group for group in scan.groups if len(group.citations) > 1]
    out.append(f"Zitatgruppen mit mehreren Schlüsseln: {len(multi)} von {len(scan.groups)} Stellen")
    out += [f"  S. {group.page + 1}: {', '.join(group.keys)}" for group in multi]
    if scan.suspected_ranges:
        out.append(
            f"  Hinweis: {scan.suspected_ranges}-mal steht zwischen zwei Zitaten nur ein "
            "Bindestrich (komprimierter Bereich wie [1–3]?). Solche Bereiche werden nicht aufgelöst."
        )
    out.append("")

    missing = [r for r in resolutions if r.status in (res.NO_ENTRY, res.NO_PDF, res.FILE_MISSING)]
    out.append(f"Quellen ohne PDF: {len(missing)} von {len(resolutions)}")
    out += [f"  {r.cite_key}: {_reason(r)}{_metadata(r)}" for r in missing]
    out.append("")

    choices = [r for r in resolutions if r.status == res.CHOICE_NEEDED]
    out.append(f"Auswahl nötig: {len(choices)} von {len(resolutions)}")
    for r in choices:
        out.append(f"  {r.cite_key}:{_metadata(r)}")
        out += [f"    {_candidate(library.items[group[0]], group)}" for group in r.candidates]
    out.append("")

    duplicates = [r for r in resolutions if r.duplicates]
    out.append(f"Dubletten in Zotero: {len(duplicates)}")
    out += [f"  {r.cite_key}: {_duplicate_note(r)}" for r in duplicates]
    out.append("")

    several_pdfs = [r for r in resolutions if r.pdf and len(r.pdfs) > 1]
    if several_pdfs:
        out.append(f"Einträge mit mehreren PDFs: {len(several_pdfs)}")
        out += [f"  {r.cite_key}: {_pdf_note(r)}" for r in several_pdfs]
        out.append("")

    warnings = [r for r in resolutions if r.conflict]
    if warnings:
        out.append(f"Widerspruch zwischen Weg A und Weg B: {len(warnings)}")
        out += [f"  {r.cite_key}: A {_way(r.way_a)}, B {_way(r.way_b)}" for r in warnings]
        out.append("")

    out += _way_summaries(resolutions, has_bib)
    return "\n".join(out)


def _way_summaries(resolutions: list[res.Resolution], has_bib: bool) -> list[str]:
    total = len(resolutions)
    out = []
    if has_bib:
        a_matched = sum(r.way_a.status == res.MATCH for r in resolutions)
        out.append(f"Weg A ordnet {a_matched} von {total} Schlüsseln zu.")
    b_matched = [r for r in resolutions if r.way_b.status == res.MATCH]
    b_choices = [r for r in resolutions if r.way_b.status == res.CHOICE]
    b_none = [r for r in resolutions if r.way_b.status == res.NONE]
    out.append(f"Weg B allein ordnet {len(b_matched)} von {total} Schlüsseln zu.")
    out.append(f"  Auswahl nötig: {len(b_choices)}")
    out += [f"    {r.cite_key}: {', '.join(r.way_b.candidates)}" for r in b_choices]
    out.append(f"  Kein Treffer: {len(b_none)}")
    out += [f"    {r.cite_key}" for r in b_none]
    if not has_bib:
        out.append("  Ohne .bib lässt sich das nicht gegen Weg A prüfen.")
        return out
    agree = sum(r.ways_agree is True for r in b_matched)
    differ = [r for r in b_matched if r.ways_agree is False]
    unchecked = [r for r in b_matched if r.ways_agree is None]
    out.append(
        f"  Von den {len(b_matched)} zugeordneten bestätigt Weg A denselben Eintrag: {agree}"
    )
    out.append(f"  Weg A findet einen anderen Eintrag: {len(differ)}")
    out += [f"    {r.cite_key}: A {_way(r.way_a)}, B {_way(r.way_b)}" for r in differ]
    out.append(f"  Weg A findet nichts Eindeutiges (nur Weg B): {len(unchecked)}")
    out += [f"    {r.cite_key}: A {_way(r.way_a)}" for r in unchecked]
    return out


def _way(way: res.WayResult) -> str:
    label = _WAY_LABELS.get(way.how, way.how)
    if way.status == res.MATCH:
        notes = [label] if label else []
        if len(way.groups[0]) > 1:
            notes.append(f"Dublette ×{len(way.groups[0])}")
        return f"{way.item_key} ({', '.join(notes)})" if notes else way.item_key
    if way.status == res.CHOICE:
        via = f" über {label}" if label else ""
        return f"Auswahl nötig{via}: {', '.join(way.candidates)}"
    if way.how == "unknown_key":
        return "–"
    return f"– ({_WAY_A_REASONS[way.how]}{_rejected(way)})"


def _rejected(way: res.WayResult) -> str:
    if way.status != res.NONE or not way.rejected:
        return ""
    return ": " + ", ".join(f"{key} im {_FIELD_LABELS[name]}" for key, name in way.rejected)


def _agreement(resolution: res.Resolution) -> str:
    return {True: "ja", False: "NEIN", None: "–"}[resolution.ways_agree]


def _reason(resolution: res.Resolution) -> str:
    if resolution.status == res.NO_PDF:
        return f"Eintrag {resolution.item_key} hat keinen PDF-Anhang"
    if resolution.status == res.FILE_MISSING:
        pdf = resolution.pdf
        where = f": {pdf.path}" if pdf.path else ""
        return f"Datei des Anhangs {pdf.attachment_key} fehlt auf der Platte{where}"
    if resolution.status == res.CHOICE_NEEDED:
        return f"Auswahl nötig ({', '.join(group[0] for group in resolution.candidates)})"
    if resolution.detail == "unknown_override":
        return f"overrides.toml verweist auf unbekannten Eintrag {resolution.override_item_key}"
    return "kein Zotero-Eintrag passt"


def _outcome(resolution: res.Resolution) -> str:
    if resolution.status != res.OK:
        return f"✗ {_reason(resolution)}"
    notes = []
    if resolution.source == "override":
        notes.append("manuell zugeordnet")
    if resolution.source == "choice":
        notes.append("gewählt")
    if resolution.duplicates:
        notes.append(f"Dublette ×{len(resolution.duplicates)}")
    if len(resolution.pdfs) > 1:
        notes.append(f"{len(resolution.pdfs)} PDFs")
    suffix = f"  [{', '.join(notes)}]" if notes else ""
    return f"{resolution.pdf_path}{suffix}"


def _duplicate_note(resolution: res.Resolution) -> str:
    others = ", ".join(resolution.duplicates[1:])
    return (
        f"Dublette: {len(resolution.duplicates)} Einträge, "
        f"gewählt {resolution.item_key} (übrige: {others})"
    )


def _pdf_note(resolution: res.Resolution) -> str:
    listed = ", ".join(
        f"{pdf.attachment_key}{'' if pdf.exists else ' (Datei fehlt)'}" for pdf in resolution.pdfs
    )
    return f"{len(resolution.pdfs)} PDFs, verwendet {resolution.pdf.attachment_key} ({listed})"


def _candidate(item: ZoteroItem, group: tuple[str, ...]) -> str:
    parts = [item.key, item.item_type, item.date or "ohne Datum"]
    parts.append(f"DOI {item.doi}" if item.doi else item.url or "ohne DOI und URL")
    parts.append(f"{len(item.pdfs)} PDF" if item.pdfs else "kein PDF")
    if len(group) > 1:
        parts.append(f"Dublette ×{len(group)}")
    return " | ".join(parts)


def _metadata(resolution: res.Resolution) -> str:
    if not resolution.title:
        return ""
    authors = resolution.authors[0] if resolution.authors else "?"
    if len(resolution.authors) > 1:
        authors += " et al."
    return f" – {resolution.title} ({authors}, {resolution.year or 'o. J.'})"


def _table(header: list[str], rows: list[list[str]]) -> str:
    # The last column holds paths and is left unpadded.
    widths = [max(len(row[i]) for row in [header, *rows]) for i in range(len(header) - 1)]
    lines = []
    for row in [header, *rows]:
        cells = [cell.ljust(width) for cell, width in zip(row, widths, strict=False)]
        lines.append("  ".join([*cells, row[-1]]).rstrip())
    lines.insert(1, "  ".join("-" * width for width in [*widths, len(header[-1])]))
    return "\n".join(lines)
