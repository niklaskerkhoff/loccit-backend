"""Minimal BibTeX reader: entries with their fields, nothing more.

It only has to read what Zotero's BibTeX translator writes (and comparable
exports), so string macros are kept as their bare name and `#` concatenation
simply joins the parts.
"""

import re
from dataclasses import dataclass, field
from pathlib import Path

_SKIPPED_TYPES = {"comment", "preamble", "string"}
_ENTRY_START = re.compile(r"@\s*([A-Za-z]+)\s*([{(])")
_FIELD_NAME = re.compile(r"\s*([^\s=,{}()\"#]+)\s*=\s*")
_BARE_VALUE = re.compile(r"[^\s,#{}()\"]+")
_YEAR = re.compile(r"\d{4}")
_NAME_SEPARATOR = re.compile(r"\s+and\s+", re.IGNORECASE)
_CLOSING = {"{": "}", "(": ")"}


@dataclass(frozen=True)
class BibEntry:
    key: str
    entry_type: str
    fields: dict[str, str]

    @property
    def title(self) -> str | None:
        return self.fields.get("title")

    @property
    def doi(self) -> str | None:
        return self.fields.get("doi")

    @property
    def year(self) -> str | None:
        for name in ("year", "date"):
            match = _YEAR.search(self.fields.get(name, ""))
            if match:
                return match.group()
        return None

    @property
    def authors(self) -> list[str]:
        names = self.fields.get("author") or self.fields.get("editor") or ""
        return [strip_braces(n).strip() for n in _NAME_SEPARATOR.split(names) if n.strip()]

    @property
    def last_names(self) -> list[str]:
        """Family names of the authors; a braced name is kept whole."""
        names = self.fields.get("author") or self.fields.get("editor") or ""
        return [_last_name(n.strip()) for n in _NAME_SEPARATOR.split(names) if n.strip()]


@dataclass
class BibFile:
    entries: dict[str, BibEntry] = field(default_factory=dict)
    # Keys defined more than once; such a key identifies nothing.
    duplicate_keys: set[str] = field(default_factory=set)


def _last_name(name: str) -> str:
    if name.startswith("{") and name.endswith("}"):
        return strip_braces(name)
    if "," in name:
        return strip_braces(name.split(",")[0]).strip()
    return strip_braces(name).split()[-1]


def strip_braces(value: str) -> str:
    return value.replace("{", "").replace("}", "")


def load_bib(path: Path) -> BibFile:
    return parse_bib(Path(path).read_text(encoding="utf-8", errors="replace"))


def parse_bib(text: str) -> BibFile:
    bib = BibFile()
    pos = 0
    while match := _ENTRY_START.search(text, pos):
        entry_type = match.group(1).lower()
        closing = _CLOSING[match.group(2)]
        pos = match.end()
        if entry_type in _SKIPPED_TYPES:
            pos = _skip_balanced(text, pos, match.group(2), closing)
            continue
        entry, pos = _parse_entry(text, pos, entry_type, closing)
        if entry is None:
            continue
        if entry.key in bib.entries:
            bib.duplicate_keys.add(entry.key)
        else:
            bib.entries[entry.key] = entry
    return bib


def _skip_balanced(text: str, pos: int, opening: str, closing: str) -> int:
    depth = 1
    while pos < len(text) and depth:
        if text[pos] == opening:
            depth += 1
        elif text[pos] == closing:
            depth -= 1
        pos += 1
    return pos


def _parse_entry(
    text: str, pos: int, entry_type: str, closing: str
) -> tuple[BibEntry | None, int]:
    end_of_key = pos
    while end_of_key < len(text) and text[end_of_key] not in ",\n" + closing:
        end_of_key += 1
    key = text[pos:end_of_key].strip()
    if not key or end_of_key >= len(text):
        return None, end_of_key
    pos = end_of_key
    fields: dict[str, str] = {}
    while pos < len(text):
        while pos < len(text) and (text[pos].isspace() or text[pos] == ","):
            pos += 1
        if pos >= len(text) or text[pos] == closing:
            pos += 1
            break
        name = _FIELD_NAME.match(text, pos)
        if not name:
            # Malformed entry: give up on it and resume at the next `@`.
            break
        value, pos = _parse_value(text, name.end())
        fields.setdefault(name.group(1).lower(), value)
    return BibEntry(key, entry_type, fields), pos


def _parse_value(text: str, pos: int) -> tuple[str, int]:
    parts = []
    while pos < len(text):
        while pos < len(text) and text[pos].isspace():
            pos += 1
        if pos >= len(text):
            break
        if text[pos] == "{":
            end = _skip_balanced(text, pos + 1, "{", "}")
            parts.append(text[pos + 1 : end - 1])
            pos = end
        elif text[pos] == '"':
            end = _end_of_quoted(text, pos + 1)
            parts.append(text[pos + 1 : end])
            pos = end + 1
        else:
            bare = _BARE_VALUE.match(text, pos)
            if not bare:
                break
            parts.append(bare.group())
            pos = bare.end()
        while pos < len(text) and text[pos].isspace():
            pos += 1
        if pos < len(text) and text[pos] == "#":
            pos += 1
            continue
        break
    return "".join(parts), pos


def _end_of_quoted(text: str, pos: int) -> int:
    depth = 0
    while pos < len(text):
        char = text[pos]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
        elif char == '"' and depth <= 0:
            break
        pos += 1
    return pos
