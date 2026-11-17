# loccit backend

loccit checks the citations of a LaTeX thesis against the cited papers: click
a citation in the thesis PDF and see the cited paper next to it, with the
supporting passages highlighted. The papers come from the local Zotero library.

Status: milestone M1 (two viewers, no AI yet).

## Setup

```sh
conda activate loccit-backend
pip install -r requirements.txt
pip install -e .
pytest
```

## Start

```sh
python -m loccit                              # start the server, open the browser
python -m loccit thesis.pdf --bib refs.bib    # same, and open this thesis right away
```

The backend serves the HTTP API on `http://127.0.0.1:8765` (`--port` to
change) and, if it knows where the built
[web frontend](../loccit-web-frontend) is, the frontend as well:

```sh
# once, after `npm run build` in loccit-web-frontend
python -m loccit --frontend /path/to/loccit-web-frontend/dist
```

The folder is remembered in `~/.loccit/settings.toml`. Without it the backend
only serves the API and prints how to start the frontend's dev server.

Zotero must be running with *Settings → Advanced → Allow other applications
on this computer to communicate with Zotero* enabled. Only GET requests are
sent to Zotero.

## Check the mapping on the command line

```sh
python -m loccit resolve thesis.pdf --bib refs.bib
```

Prints, for every cite key in the thesis PDF, what way A and way B found,
whether they agree, and the PDF path reported by Zotero or the reason why
there is none.

## HTTP API

Documented by its OpenAPI schema: `/api/openapi.json`, browsable at
`/api/docs` while the server runs.

| Method and path | Purpose |
| --- | --- |
| `GET /api/documents` | stored theses, most recently opened first |
| `POST /api/documents` | upload a thesis PDF and optionally its `.bib` |
| `DELETE /api/documents/{id}` | remove a thesis from loccit |
| `GET /api/documents/{id}/pdf` | the thesis PDF |
| `GET /api/documents/{id}/citations` | citation groups and what each cite key resolves to; `?reload=true` reads Zotero again |
| `GET /api/papers/{attachment_key}/pdf` | a paper's PDF |
| `PUT /api/choices/items/{cite_key}` | remember which version of a publication a key stands for |
| `PUT /api/choices/pdfs/{item_key}` | remember which of an item's PDFs to show |

The server listens on 127.0.0.1 only, refuses requests for other host names,
and serves a paper only if it resolved a citation to that attachment itself;
there is no way to request a file by path.

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

Everything loccit stores lives in `~/.loccit/`; delete the folder to get rid
of it.

| Path | Content |
| --- | --- |
| `loccit.sqlite` | list of uploaded theses, choices between versions and PDFs |
| `documents/<id>/` | `thesis.pdf` and `refs.bib` of an uploaded thesis |
| `settings.toml` | `frontend_dist`, the folder of the built frontend |
| `overrides.toml` | optional, written by hand: cite key to Zotero item key |

Uploading a PDF with the same file name (a browser's ` (1)` download counter
is ignored) replaces the stored version; without a new `.bib` the stored one
is kept. Paper PDFs are read where Zotero keeps them and are never copied.
