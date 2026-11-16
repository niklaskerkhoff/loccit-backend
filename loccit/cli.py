"""Command line entry point: `python -m loccit <command>`."""

import argparse
import sys
from pathlib import Path

from loccit import paths, resolve as res
from loccit.bibfile import load_bib
from loccit.citations import CitationScan, scan_citations
from loccit.zotero import (
    KEY_FROM_EXPORT,
    LibraryIndex,
    ZoteroApiDisabled,
    ZoteroClient,
    ZoteroError,
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
}
_WAY_A_MATCHES = {"doi": "DOI", "title+year": "Titel+Jahr"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m loccit")
    commands = parser.add_subparsers(dest="command", required=True)
    resolve = commands.add_parser(
        "resolve", help="show how every citation of a thesis PDF maps to a Zotero PDF"
    )
    resolve.add_argument("pdf", type=Path, help="PDF of the thesis")
    resolve.add_argument("--bib", type=Path, help=".bib file of the thesis")
    args = parser.parse_args(argv)
    return run_resolve(args.pdf, args.bib)


def run_resolve(pdf: Path, bib_path: Path | None) -> int:
    for path in (pdf, bib_path):
        if path and not path.is_file():
            print(f"Datei nicht gefunden: {path}", file=sys.stderr)
            return 2

    scan = scan_citations(pdf)
    if not scan.groups:
        print(_no_citation_links(scan), file=sys.stderr)
        return 1

    client = ZoteroClient()
    try:
        library = client.load_library()
        bib = load_bib(bib_path) if bib_path else None
        overrides = res.load_overrides(paths.overrides_file())
        resolutions = [
            res.resolve(
                key,
                bib=bib,
                library=library,
                overrides=overrides,
                locate_file=client.attachment_file_url,
            )
            for key in scan.keys
        ]
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

    print(_report(scan, library, resolutions, has_bib=bib is not None))
    return 0


def _no_citation_links(scan: CitationScan) -> str:
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
                [r.cite_key, _way_a(r.way_a), _way_b(r.way_b), _agreement(r), _outcome(r)]
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

    missing = [r for r in resolutions if r.status != res.OK]
    out.append(f"Quellen ohne PDF: {len(missing)} von {len(resolutions)}")
    out += [f"  {r.cite_key}: {_reason(r)}{_metadata(r)}" for r in missing]
    out.append("")

    warnings = [r for r in resolutions if r.conflict]
    if warnings:
        out.append(f"Widerspruch zwischen Weg A und Weg B: {len(warnings)}")
        out += [f"  {r.cite_key}: A {_way_a(r.way_a)}, B {_way_b(r.way_b)}" for r in warnings]
        out.append("")

    out += _way_b_summary(resolutions, has_bib)
    return "\n".join(out)


def _way_b_summary(resolutions: list[res.Resolution], has_bib: bool) -> list[str]:
    matched = [r for r in resolutions if r.way_b.status == res.MATCH]
    out = [f"Weg B allein ordnet {len(matched)} von {len(resolutions)} Schlüsseln eindeutig zu."]
    if not has_bib:
        out.append("  Ohne .bib lässt sich das nicht gegen Weg A prüfen.")
        return out
    agree = [r for r in matched if r.ways_agree]
    differ = [r for r in matched if r.ways_agree is False]
    unchecked = [r for r in matched if r.ways_agree is None]
    out.append(f"  Davon bestätigt Weg A denselben Eintrag: {len(agree)}")
    out.append(f"  Weg A findet einen anderen Eintrag: {len(differ)}")
    out.append(f"  Weg A findet nichts Eindeutiges (nur Weg B): {len(unchecked)}")
    out += [f"    {r.cite_key}: A {_way_a(r.way_a)}" for r in unchecked]
    lost = [r for r in resolutions if r.way_b.status != res.MATCH and r.way_a.status == res.MATCH]
    out.append(f"  Nur Weg A findet den Eintrag (ohne .bib verloren): {len(lost)}")
    out += [f"    {r.cite_key}: B {_way_b(r.way_b)}" for r in lost]
    return out


def _way_a(way: res.WayResult) -> str:
    if way.status == res.MATCH:
        return f"{way.item_key} ({_WAY_A_MATCHES[way.how]})"
    if way.status == res.AMBIGUOUS:
        return f"mehrdeutig über {_WAY_A_MATCHES[way.how]}: {', '.join(way.candidates)}"
    return f"– ({_WAY_A_REASONS[way.how]})"


def _way_b(way: res.WayResult) -> str:
    if way.status == res.MATCH:
        origin = "" if way.how == KEY_FROM_EXPORT else f" ({way.how})"
        return f"{way.item_key}{origin}"
    if way.status == res.AMBIGUOUS:
        return f"mehrdeutig: {', '.join(way.candidates)}"
    return "–"


def _agreement(resolution: res.Resolution) -> str:
    return {True: "ja", False: "NEIN", None: "–"}[resolution.ways_agree]


def _reason(resolution: res.Resolution) -> str:
    if resolution.status == res.NO_PDF:
        return f"Eintrag {resolution.item_key} hat keinen PDF-Anhang"
    if resolution.status == res.FILE_MISSING:
        where = f": {resolution.pdf_path}" if resolution.pdf_path else ""
        return f"Datei des Anhangs {resolution.attachment_key} fehlt auf der Platte{where}"
    if resolution.detail == "unknown_override":
        return f"overrides.toml verweist auf unbekannten Eintrag {resolution.override_item_key}"
    if resolution.detail == "ambiguous":
        candidates = dict.fromkeys(resolution.way_a.candidates + resolution.way_b.candidates)
        return f"mehrere Zotero-Einträge passen ({', '.join(candidates)})"
    return "kein Zotero-Eintrag passt"


def _outcome(resolution: res.Resolution) -> str:
    if resolution.status != res.OK:
        return f"✗ {_reason(resolution)}"
    notes = []
    if resolution.source == "override":
        notes.append("manuell zugeordnet")
    if resolution.other_pdf_count:
        notes.append(f"+{resolution.other_pdf_count} weitere PDFs")
    suffix = f"  [{', '.join(notes)}]" if notes else ""
    return f"{resolution.pdf_path}{suffix}"


def _metadata(resolution: res.Resolution) -> str:
    if not resolution.title:
        return ""
    # .bib names are "Last, First"; Zotero names are last names already.
    authors = resolution.authors[0].split(",")[0] if resolution.authors else "?"
    if len(resolution.authors) > 1:
        authors += " et al."
    return f" – {resolution.title} ({authors}, {resolution.year or 'o. J.'})"


def _table(header: list[str], rows: list[list[str]]) -> str:
    # The last column holds paths and is left unpadded.
    widths = [max(len(row[i]) for row in [header, *rows]) for i in range(len(header) - 1)]
    lines = []
    for row in [header, *rows]:
        cells = [cell.ljust(width) for cell, width in zip(row, widths)]
        lines.append("  ".join([*cells, row[-1]]).rstrip())
    lines.insert(1, "  ".join("-" * width for width in [*widths, len(header[-1])]))
    return "\n".join(lines)
