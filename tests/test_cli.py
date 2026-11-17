import pytest

from loccit import cli
from loccit.zotero import LibraryIndex, ZoteroApiDisabled, ZoteroNotRunning
from tests.helpers import make_pdf, raw_item, raw_pdf
from tests.test_resolve import bib_entry


class FakeZotero:
    """Stands in for ZoteroClient; papers are files in `folder`."""

    raw_items: list[dict] = []
    folder = None
    error: Exception | None = None

    def load_library(self) -> LibraryIndex:
        if self.error:
            raise self.error
        return LibraryIndex.from_api_items(self.raw_items)

    def attachment_file_url(self, attachment_key: str) -> str | None:
        return (self.folder / f"{attachment_key}.pdf").as_uri()

    def close(self) -> None:
        pass


@pytest.fixture
def zotero(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "ZoteroClient", FakeZotero)
    monkeypatch.setattr(cli.paths, "overrides_file", lambda: tmp_path / "overrides.toml")
    monkeypatch.setattr(FakeZotero, "folder", tmp_path)
    monkeypatch.setattr(FakeZotero, "error", None)
    monkeypatch.setattr(
        FakeZotero,
        "raw_items",
        [
            raw_item("ITEM0001", "A Study", cite_key="heltweg_study_2025", doi="10.1000/abc"),
            raw_pdf("PDF00001", "ITEM0001"),
            raw_item("ITEM0002", "No PDF here", cite_key="brown_no_2020", year="2020"),
        ],
    )
    (tmp_path / "PDF00001.pdf").write_bytes(b"%PDF-1.7")
    return FakeZotero


@pytest.fixture
def thesis(tmp_path):
    pdf = make_pdf(
        tmp_path / "thesis.pdf",
        [
            [
                [
                    "Claim [",
                    ("1", "cite.heltweg_study_2025"),
                    ", ",
                    ("2", "cite.brown_no_2020"),
                    "].",
                ],
                ["Another [", ("3", "cite.missing_key_2019"), "]."],
            ]
        ],
    )
    bib = tmp_path / "refs.bib"
    bib.write_text(
        bib_entry("heltweg_study_2025", "A Study", doi="10.1000/abc")
        + bib_entry("brown_no_2020", "No {PDF} here", year="2020")
        + bib_entry("missing_key_2019", "Never added to Zotero", year="2019")
    )
    return pdf, bib


def test_resolve_report(zotero, thesis, tmp_path, capsys):
    pdf, bib = thesis
    assert cli.main(["resolve", str(pdf), "--bib", str(bib)]) == 0
    out = capsys.readouterr().out
    assert "3 Zitate an 2 Stellen, 3 verschiedene Schlüssel" in out
    rows = {line.split()[0]: line.split()[1:] for line in out.splitlines() if line.strip()}
    assert rows["heltweg_study_2025"] == [
        "ITEM0001",
        "(DOI)",
        "ITEM0001",
        "ja",
        str(tmp_path / "PDF00001.pdf"),
    ]
    assert "S. 1: heltweg_study_2025, brown_no_2020" in out
    assert "Quellen ohne PDF: 2 von 3" in out
    assert "brown_no_2020: Eintrag ITEM0002 hat keinen PDF-Anhang – No PDF here" in out
    assert "Auswahl nötig: 0 von 3" in out
    assert "Dubletten in Zotero: 0" in out
    assert "Weg A ordnet 2 von 3 Schlüsseln zu." in out
    assert (
        "missing_key_2019: kein Zotero-Eintrag passt – Never added to Zotero "
        "(Heltweg et al., 2019)" in out
    )
    assert "Weg B allein ordnet 2 von 3 Schlüsseln zu." in out
    assert "Von den 2 zugeordneten bestätigt Weg A denselben Eintrag: 2" in out


def test_resolve_without_bib(zotero, thesis, capsys):
    pdf, _ = thesis
    assert cli.main(["resolve", str(pdf)]) == 0
    out = capsys.readouterr().out
    assert "– (keine .bib)" in out
    assert "Ohne .bib lässt sich das nicht gegen Weg A prüfen." in out


def test_pdf_without_citation_links(zotero, tmp_path, capsys):
    pdf = make_pdf(tmp_path / "plain.pdf", [[["See ", ("2.1", "section.2.1"), "."]]])
    assert cli.main(["resolve", str(pdf)]) == 1
    err = capsys.readouterr().err
    assert "enthält keine Zitat-Links" in err
    assert "section.* (1)" in err


@pytest.mark.parametrize(
    ("error", "message"),
    [(ZoteroNotRunning("refused"), "Zotero starten"), (ZoteroApiDisabled(), "Einstellungen")],
)
def test_zotero_problems_are_explained(zotero, thesis, capsys, error, message):
    zotero.error = error
    pdf, bib = thesis
    assert cli.main(["resolve", str(pdf), "--bib", str(bib)]) == 2
    assert message in capsys.readouterr().err


def test_missing_input_file(zotero, tmp_path, capsys):
    assert cli.main(["resolve", str(tmp_path / "nope.pdf")]) == 2
    assert "Datei nicht gefunden" in capsys.readouterr().err


# --- API key and model check -------------------------------------------------


@pytest.fixture
def keychain(monkeypatch):
    stored: dict[tuple[str, str], str] = {}

    def delete(service, user):
        if (service, user) not in stored:
            raise cli.models.keyring.errors.PasswordDeleteError()
        del stored[(service, user)]

    monkeypatch.setattr(cli.models.keyring, "get_password", lambda s, u: stored.get((s, u)))
    monkeypatch.setattr(
        cli.models.keyring, "set_password", lambda s, u, k: stored.update({(s, u): k})
    )
    monkeypatch.setattr(cli.models.keyring, "delete_password", delete)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    return stored


def test_set_key_stores_the_key_in_the_keychain_without_echoing_it(keychain, monkeypatch, capsys):
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt: "  sk-ant-secret  ")
    assert cli.main(["set-key"]) == 0
    assert cli.models.get_api_key() == "sk-ant-secret"
    output = capsys.readouterr()
    assert "sk-ant-secret" not in output.out + output.err
    assert cli.main(["set-key", "--delete"]) == 0
    assert cli.models.get_api_key() is None
    assert cli.main(["set-key", "--delete"]) == 0


def test_set_key_ignores_empty_input(keychain, monkeypatch):
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt: "   ")
    assert cli.main(["set-key"]) == 2
    assert keychain == {}


class FakeModelsApi:
    """Answers `client.models.retrieve` like the Models API."""

    def __init__(self, off: dict[str, str | None]):
        self.off = off
        self.models = self

    def retrieve(self, model_id: str):
        from anthropic.types import ModelInfo

        def support(value: bool) -> dict:
            return {"supported": value}

        types = {
            name: support(self.off[model_id] == name) for name in ("disabled", "between_tools")
        }
        types |= {"adaptive": support(True), "enabled": support(False)}
        effort = {level: support(True) for level in ("low", "medium", "high", "xhigh", "max")}
        capabilities = {
            name: support(True)
            for name in (
                "batch",
                "citations",
                "code_execution",
                "image_input",
                "pdf_input",
                "structured_outputs",
            )
        }
        capabilities["effort"] = {"supported": True, **effort}
        capabilities["thinking"] = {"supported": True, "types": types}
        return ModelInfo.model_construct(id=model_id, capabilities=_Capabilities(capabilities))


class _Capabilities:
    def __init__(self, tree: dict):
        self.tree = tree

    def model_dump(self) -> dict:
        return self.tree


def test_check_models_confirms_the_defaults(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli.paths, "models_file", lambda: tmp_path / "models.toml")
    api = FakeModelsApi(
        {
            "claude-sonnet-5-5": "between_tools",
            "claude-opus-5-5": None,
            "claude-haiku-5-5": "disabled",
            "claude-fable-5-2": None,
        }
    )
    assert cli.run_check_models(api) == 0
    assert capsys.readouterr().out.count("passt zur API") == 4


def test_check_models_reports_what_the_api_sees_differently(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli.paths, "models_file", lambda: tmp_path / "models.toml")
    api = FakeModelsApi(
        {
            "claude-sonnet-5-5": "disabled",
            "claude-opus-5-5": "between_tools",
            "claude-haiku-5-5": "disabled",
            "claude-fable-5-2": None,
        }
    )
    assert cli.run_check_models(api) == 1
    out = capsys.readouterr().out
    assert "✗ claude-sonnet-5-5: Stufe „off“ wird laut API nicht unterstützt" in out
    assert '✗ claude-opus-5-5: „off“ wäre möglich (thinking_off = "between_tools")' in out
    assert "✓ claude-haiku-5-5" in out
