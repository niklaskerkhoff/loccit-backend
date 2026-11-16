# loccit backend

loccit checks the citations of a LaTeX thesis against the cited papers: click
a citation in the thesis PDF and see the cited paper next to it, with the
supporting passages highlighted. The papers come from the local Zotero library.

Status: milestone M0 (mapping citations to Zotero PDFs, command line only).

## Setup

```sh
conda activate loccit-backend
pip install -r requirements.txt
pip install -e .
pytest
```

## Check the mapping

```sh
python -m loccit resolve thesis.pdf --bib refs.bib
```

Prints, for every cite key in the thesis PDF, what way A and way B found,
whether they agree, and the PDF path reported by Zotero or the reason why
there is none. Zotero must be running with *Settings → Advanced → Allow other
applications on this computer to communicate with Zotero* enabled. Only GET
requests are sent to Zotero.

## How a citation is mapped to a PDF

Every step is an exact lookup. There is no fuzzy matching and no language
model involved.

1. **Cite key from the thesis PDF.** Citation links are link annotations with
   the named destination `cite.<key>` (natbib/BibTeX) or
   `cite.<refsection>@<key>` (biblatex).
2. **Cite key to Zotero item**, in this order of precedence:
   - **Manual override** in `~/.loccit/overrides.toml`:
     ```toml
     [overrides]
     heltweg_empirical_2025 = "BP9IBQGJ"   # cite key = Zotero item key
     ```
   - **Way A, via the `.bib`:** look the entry up by DOI, then by URL, then
     by title and year (all normalised, all exact). Items that contradict the
     entry in DOI, URL or entry type are dropped; an equal DOI settles it
     regardless of type and URL.
   - **Way B, via Zotero's own keys:** the key must equal the key that
     Zotero's BibTeX translator produces for an item, or an item's
     `citationKey` field, or a `Citation Key:` line in its Extra field. A key
     with a suffix (`key-1`, as a whole-library export names items with the
     same key) is traced back to `key`.

   If A and B find different publications, A wins and the conflict is
   reported.
3. **More than one item left.**
   - Items that cannot be told apart are **duplicates**: they share a DOI, or
     have no DOI and agree in title, year, item type and URL. loccit picks
     one by a fixed rule (an item whose PDF is on disk first, among those the
     one added last) and says so.
   - Items that can be told apart are **different versions** (say, a preprint
     and the conference paper). loccit never guesses between them: the status
     is "choice needed" and the candidates are listed.
4. **Zotero item to file.** The attachment Zotero itself treats as the item's
   main one (`links.attachment`) is used; if its file is missing, the next
   PDF whose file is there. The location is asked from Zotero
   (`/items/<key>/file/view/url`), never guessed.

A paper can be unavailable for three distinct reasons: no Zotero item
matches, the item has no PDF attachment, or the files of its attachments are
missing on disk.

## Known limitations

- **Compressed numeric ranges** such as `[1–3]` only link their end points.
  The keys in between are not recovered; `resolve` reports how often this
  pattern occurs.
- **Citation groups** are found per page. A `\cite{a,b}` that is split by a
  page break becomes two groups.

## Data

Everything loccit stores lives in `~/.loccit/`. Paper PDFs are read where
Zotero keeps them and are never copied.
