# loccit backend

loccit checks the citations of a LaTeX thesis against the cited papers: click
a citation in the thesis PDF and see the cited paper next to it, with the
supporting passages highlighted. The papers come from the local Zotero library.

Status: milestone M2 (passages found by a Claude model, marked in the paper).

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

### API key

Checking citations needs an Anthropic API key. It is read from the
environment variable `ANTHROPIC_API_KEY` or, if that is not set, from the
system keychain:

```sh
python -m loccit set-key            # asks for the key, stores it in the keychain
python -m loccit set-key --delete
```

The key is never written to a file or a log. Without a key loccit still shows
the papers; it just cannot check.

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
| `GET /api/models` | models and thinking levels on offer, whether an API key is set |
| `GET /api/documents/{id}/groups/{group}/check` | the latest stored run for a citation group, model and thinking level |
| `POST /api/documents/{id}/groups/{group}/check` | check now and store the result as a new run |
| `PUT /api/choices/items/{cite_key}` | remember which version of a publication a key stands for |
| `PUT /api/choices/pdfs/{item_key}` | remember which of an item's PDFs to show |

The server listens on 127.0.0.1 only, refuses requests for other host names,
and serves a paper only if it resolved a citation to that attachment itself;
there is no way to request a file by path.

## How a citation is checked

1. **The checked text** is the paragraph of the thesis around the citation,
   with the citation marked (`<cite_here>…</cite_here>`). Line breaks and
   hyphenation are undone. The UI can show exactly this text.
2. **The papers** are read whole, never in retrieved chunks. PyMuPDF text is
   split into sentences; every sentence keeps its page and one rectangle per
   line. Short fragments (table cells, headings) are collected into one unit.
3. **The model points, it does not quote.** Strategies implement
   `find_passages(context, papers, model, thinking) -> Result`
   (`loccit/strategies/`). The result is a verdict (`supported`,
   `partially_supported`, `not_found`) and passages as sentence ranges,
   strongest first. Highlights are drawn from the stored rectangles of those
   sentences only; text written by the model is never used for them.
4. **Strategy `claude-citations`:** each paper is a "custom content" document
   with one block per sentence and citations enabled; all available papers of
   a citation group go into one request. Citations cannot be combined with
   structured outputs, so the answer is a plain line format (`VERDICT: …`,
   `PASSAGE: …`), and the block indices of the citation attached to a
   `PASSAGE` line are the sentence range. The prompt asks for `not_found`
   rather than loosely related passages. The backend waits for the complete
   answer; nothing is streamed to the UI.
5. **No paper, no check.** A source without a readable PDF is never sent to a
   model and gets no verdict. In a group, the other papers are checked and
   the result says which sources it does not cover.

### Models and thinking levels

`~/.loccit/models.toml` lists the models on offer, their prices and the
thinking levels each one accepts. It is written with defaults on the first
start and read at every start, so a new model id or price is an edit of that
file. `python -m loccit check-models` compares the file with what the Models
API reports for your key.

Defaults (documentation as of 2026-11-17): `claude-sonnet-5-5` (default),
`claude-opus-5-5`, `claude-haiku-5-5`, `claude-fable-5-2`. Thinking "off" is
offered where the API allows it: Sonnet 5.5 (`thinking.type =
"between_tools"`) and Haiku 5.5 (`"disabled"`). Thinking is always on for
Opus 5.5 and Fable 5.2, whose lowest level is effort `low`. All levels work
together with citations.

Refusal fallbacks to another model are deliberately not enabled: a run must
come from the model it is stored under. A refusal is reported as an error.

### Stored runs

Every check is stored and none is overwritten.

- A **task** is what was checked: the hash of the marked paragraph, the cite
  keys of the group, and the papers by file hash. All runs of the same task
  hang on the same row, whatever the model. A changed paragraph, another PDF
  file or a missing paper that was added makes a new task; a new version of
  the thesis with the same paragraph does not.
- A **run** records model id, thinking level, strategy, prompt version, start
  time, duration, token counts (including cache reads and writes), estimated
  cost, the verdict and the model's raw answer.
- The **passages** of a run are stored as sentence ranges per paper, in order
  of strength, so that runs can later be compared sentence by sentence.

A click on a citation shows the latest run for the selected model and
thinking level. The first click on a citation with no stored run starts a
check; changing model or level never does. "Neu prüfen" adds a run.

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
- **Paragraphs** are recognised from the layout: a new paragraph starts at an
  indented line or after a short line that ends a sentence. A paragraph that
  continues on the next page is cut at the page break.
- **Sentences** are split by rule (punctuation, capitals, a list of
  abbreviations). A sentence that runs across a column or page break becomes
  two units; a model can still cite both as one range.
- **Scanned PDFs** without a text layer cannot be checked (no OCR).

## Data

Everything loccit stores lives in `~/.loccit/`; delete the folder to get rid
of it.

| Path | Content |
| --- | --- |
| `loccit.sqlite` | uploaded theses, choices between versions and PDFs, tasks, runs and passages |
| `documents/<id>/` | `thesis.pdf` and `refs.bib` of an uploaded thesis |
| `settings.toml` | `frontend_dist`, the folder of the built frontend |
| `models.toml` | models, prices and thinking levels; edit to change them |
| `overrides.toml` | optional, written by hand: cite key to Zotero item key |

Uploading a PDF with the same file name (a browser's ` (1)` download counter
is ignored) replaces the stored version; without a new `.bib` the stored one
is kept. Paper PDFs are read where Zotero keeps them and are never copied.

The only data that leaves the machine are the requests to the Claude API:
the paragraph of the thesis and the text of the cited papers.
