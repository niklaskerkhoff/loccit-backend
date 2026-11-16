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
    out.append(f"  Von den {len(b_matched)} zugeordneten bestätigt Weg A denselben Eintrag: {agree}")
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
