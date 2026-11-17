import pytest
from fastapi.testclient import TestClient

from loccit.api import create_app
from loccit.workspace import Workspace
from tests.helpers import FakeZotero, make_pdf, raw_item, raw_pdf
from tests.test_resolve import brown


@pytest.fixture
def zotero(tmp_path):
    papers = tmp_path / "zotero"
    papers.mkdir()
    fake = FakeZotero(
        papers,
        [
            raw_item(
                "ITEM0001",
                "A Study",
                cite_key="heltweg_study_2025",
                doi="10.1000/abc",
                main_attachment="PDF00001",
            ),
            raw_pdf("PDF00001", "ITEM0001"),
            raw_pdf("PDF00002", "ITEM0001", date_added="2025-01-01T00:00:00Z"),
            *brown(),
        ],
    )
    for attachment_key in ("PDF00001", "PDF00002", "RR84UWN7", "MWZL9XFR"):
        fake.add_paper(attachment_key)
    return fake


@pytest.fixture
def client(tmp_path, zotero):
    app = create_app(Workspace(tmp_path / "home", zotero))
    return TestClient(app, base_url="http://localhost")


@pytest.fixture
def thesis_pdf(tmp_path):
    path = make_pdf(
        tmp_path / "thesis.pdf",
        [
            [
                [
                    "A [",
                    ("1", "cite.heltweg_study_2025"),
                    ", ",
                    ("2", "cite.brown_language_2020"),
                    "].",
                ]
            ]
        ],
    )
    return path.read_bytes()


def upload(client, pdf: bytes, bib: bytes | None = None, name: str = "thesis.pdf") -> dict:
    files = {"pdf": (name, pdf, "application/pdf")}
    if bib is not None:
        files["bib"] = ("refs.bib", bib, "text/plain")
    response = client.post("/api/documents", files=files)
    assert response.status_code == 200, response.text
    return response.json()


def test_upload_list_and_download(client, thesis_pdf):
    assert client.get("/api/documents").json() == []
    document = upload(client, thesis_pdf, b"@misc{a}")
    assert (document["name"], document["has_bib"]) == ("thesis.pdf", True)
    assert [d["id"] for d in client.get("/api/documents").json()] == [document["id"]]
    response = client.get(f"/api/documents/{document['id']}/pdf")
    assert response.headers["content-type"] == "application/pdf"
    assert response.content == thesis_pdf


def test_upload_of_a_newer_version_replaces_the_document(client, thesis_pdf, tmp_path):
    first = upload(client, thesis_pdf)
    newer = make_pdf(tmp_path / "v2.pdf", [[["Only [", ("1", "cite.other_2024"), "]."]]])
    second = upload(client, newer.read_bytes())
    assert second["id"] == first["id"]
    assert second["version"] != first["version"]
    assert len(client.get("/api/documents").json()) == 1


def test_upload_rejects_what_is_not_a_pdf(client):
    response = client.post("/api/documents", files={"pdf": ("thesis.pdf", b"nope")})
    assert response.status_code == 422


def test_citations(client, thesis_pdf):
    document = upload(client, thesis_pdf)
    body = client.get(f"/api/documents/{document['id']}/citations").json()
    assert body["zotero"] == {"status": "ok", "detail": None}
    assert body["named_destinations"] == {"cite": 2}
    (group,) = body["groups"]
    assert group["page"] == 0
    assert [c["key"] for c in group["citations"]] == ["heltweg_study_2025", "brown_language_2020"]
    assert len(group["citations"][0]["rects"][0]) == 4
    assert group["citations"][0]["annotation_ids"][0].endswith("R")

    heltweg = body["sources"]["heltweg_study_2025"]
    assert (heltweg["status"], heltweg["chosen_by"]) == ("ok", "b")
    assert (heltweg["title"], heltweg["authors"], heltweg["year"]) == (
        "A Study",
        ["Heltweg"],
        "2025",
    )
    assert heltweg["attachment_key"] == "PDF00001"
    assert [pdf["attachment_key"] for pdf in heltweg["pdfs"]] == ["PDF00001", "PDF00002"]

    brown_source = body["sources"]["brown_language_2020"]
    assert brown_source["status"] == "choice_needed"
    assert brown_source["attachment_key"] is None
    assert [
        (c["item_key"], c["item_type"], c["doi"], c["pdf_count"])
        for c in brown_source["candidates"]
    ] == [
        ("K38PK3JU", "preprint", "10.48550/arXiv.2005.14165", 1),
        ("SFTVNYYH", "conferencePaper", None, 1),
    ]


def test_choosing_a_version_and_a_pdf(client, thesis_pdf):
    document = upload(client, thesis_pdf)
    url = f"/api/documents/{document['id']}/citations"
    client.get(url)

    assert (
        client.put(
            "/api/choices/items/brown_language_2020", json={"item_key": "SFTVNYYH"}
        ).status_code
        == 204
    )
    brown_source = client.get(url).json()["sources"]["brown_language_2020"]
    assert (brown_source["status"], brown_source["chosen_by"]) == ("ok", "choice")
    assert brown_source["attachment_key"] == "MWZL9XFR"
    assert len(brown_source["candidates"]) == 2

    client.put("/api/choices/items/brown_language_2020", json={"item_key": None})
    assert client.get(url).json()["sources"]["brown_language_2020"]["status"] == "choice_needed"

    client.put("/api/choices/pdfs/ITEM0001", json={"attachment_key": "PDF00002"})
    assert client.get(url).json()["sources"]["heltweg_study_2025"]["attachment_key"] == "PDF00002"


def test_papers_are_served_only_after_they_were_resolved(client, thesis_pdf, zotero):
    document = upload(client, thesis_pdf)
    assert client.get("/api/papers/PDF00001/pdf").status_code == 404
    client.get(f"/api/documents/{document['id']}/citations")
    response = client.get("/api/papers/PDF00001/pdf")
    assert response.status_code == 200
    assert response.content == (zotero.folder / "PDF00001.pdf").read_bytes()
    # In Zotero, but not what any citation resolved to.
    assert client.get("/api/papers/MWZL9XFR/pdf").status_code == 404
    assert client.get("/api/papers/..%2F..%2Fthesis.pdf/pdf").status_code == 404


def test_zotero_not_running_is_reported_with_the_citations(client, thesis_pdf, zotero):
    from loccit.zotero import ZoteroNotRunning

    zotero.error = ZoteroNotRunning("refused")
    document = upload(client, thesis_pdf)
    body = client.get(f"/api/documents/{document['id']}/citations").json()
    assert body["zotero"]["status"] == "not_running"
    assert len(body["groups"]) == 1
    assert body["sources"] == {}


def test_unknown_document(client):
    assert client.get("/api/documents/7/citations").status_code == 404
    assert client.get("/api/documents/7/pdf").status_code == 404
    assert client.delete("/api/documents/7").status_code == 404


def test_delete_document(client, thesis_pdf):
    document = upload(client, thesis_pdf)
    assert client.delete(f"/api/documents/{document['id']}").status_code == 204
    assert client.get("/api/documents").json() == []


def test_requests_for_another_host_are_refused(client):
    response = client.get("/api/documents", headers={"host": "evil.example"})
    assert response.status_code == 400


def test_openapi_schema_describes_the_api(client):
    schema = client.get("/api/openapi.json").json()
    assert set(schema["paths"]) == {
        "/api/documents",
        "/api/documents/{document_id}",
        "/api/documents/{document_id}/pdf",
        "/api/documents/{document_id}/citations",
        "/api/papers/{attachment_key}/pdf",
        "/api/models",
        "/api/documents/{document_id}/groups/{group_id}/check",
        "/api/choices/items/{cite_key}",
        "/api/choices/pdfs/{item_key}",
    }


def test_built_frontend_is_served_next_to_the_api(tmp_path, zotero):
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<title>loccit</title>")
    (dist / "assets" / "app.js").write_text("console.log('loccit')")
    app = create_app(Workspace(tmp_path / "home", zotero), frontend_dist=dist)
    client = TestClient(app, base_url="http://localhost")
    assert "<title>loccit</title>" in client.get("/").text
    assert client.get("/assets/app.js").status_code == 200
    assert client.get("/api/documents").json() == []
