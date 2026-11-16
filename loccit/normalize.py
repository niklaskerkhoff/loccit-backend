"""Normalisation used for exact matching of DOIs and titles.

Nothing here is fuzzy: two values match only if their normal forms are equal.
"""

import re
import unicodedata

_DOI_PREFIX = re.compile(r"^(https?://(dx\.)?doi\.org/|doi:\s*)", re.IGNORECASE)
_LATEX_ESCAPE = re.compile(r"\\([_%&#$])")
_LATEX_COMMAND = re.compile(r"\\([a-zA-Z]+\*?|.)")
_NON_ALNUM = re.compile(r"[\W_]+")


def normalize_doi(doi: str | None) -> str | None:
    if not doi:
        return None
    doi = _LATEX_ESCAPE.sub(r"\1", doi).replace("{", "").replace("}", "")
    doi = _DOI_PREFIX.sub("", doi.strip()).strip().lower()
    return doi or None


def normalize_url(url: str | None) -> str | None:
    if not url:
        return None
    return _LATEX_ESCAPE.sub(r"\1", url).strip() or None


def normalize_title(title: str | None) -> str:
    """Lower-case, without braces, LaTeX commands, punctuation and repeated spaces."""
    if not title:
        return ""
    # `\&` is a literal character; every other command (`\textit`, `\"`) is dropped,
    # so that `{\"u}` and `ü` agree after the accent folding below.
    title = _LATEX_ESCAPE.sub(r"\1", title)
    title = _LATEX_COMMAND.sub("", title)
    title = title.replace("{", "").replace("}", "")
    title = unicodedata.normalize("NFKD", title)
    title = "".join(c for c in title if not unicodedata.combining(c))
    title = _NON_ALNUM.sub(" ", title.casefold())
    return title.strip()
