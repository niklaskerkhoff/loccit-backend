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
model involved; whatever is not a unique hit counts as not found.

1. **Cite key from the thesis PDF.** Citation links are link annotations with
   the named destination `cite.<key>` (natbib/BibTeX) or
   `cite.<refsection>@<key>` (biblatex).
2. **Cite key to Zotero item**, in this order of precedence:
   - **Manual override** in `~/.loccit/overrides.toml`:
     ```toml
     [overrides]
     heltweg_empirical_2025 = "BP9IBQGJ"   # cite key = Zotero item key
     ```
   - **Way A, via the `.bib`:** the entry's DOI (normalised) must equal the
     DOI of exactly one Zotero item; otherwise its title (normalised) and year
     must equal those of exactly one item.
   - **Way B, via Zotero's own keys:** the key must equal the key that
     Zotero's BibTeX translator produces for exactly one item, or an item's
     `citationKey` field, or a `Citation Key:` line in its Extra field.

   If A and B find different items, A wins and the conflict is reported.
3. **Zotero item to file.** Of the item's PDF attachments, the one added first
   is used. Its location is asked from Zotero
   (`/items/<key>/file/view/url`), never guessed.

A paper can be unavailable for three distinct reasons: no (or more than one)
Zotero item matches, the item has no PDF attachment, or the attachment's file
is missing on disk.

## Known limitations

- **Duplicates in Zotero.** If the library holds the same publication twice,
  both ways see two candidates and the key stays unresolved. A whole-library
  BibTeX export names such duplicates `key`, `key-1`, …; which item gets which
  suffix depends on the export order, so suffixed keys are not matched by
  way B. Merge the duplicates in Zotero or add an override.
- **Compressed numeric ranges** such as `[1–3]` only link their end points.
  The keys in between are not recovered; `resolve` reports how often this
  pattern occurs.
- **Citation groups** are found per page. A `\cite{a,b}` that is split by a
  page break becomes two groups.

## Data

Everything loccit stores lives in `~/.loccit/`. Paper PDFs are read where
Zotero keeps them and are never copied.
