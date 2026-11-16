import httpx
import pytest

from loccit.zotero import (
    KEY_FROM_EXPORT,
    KEY_FROM_EXTRA,
    KEY_FROM_FIELD,
    LibraryIndex,
    ZoteroApiDisabled,
    ZoteroClient,
    ZoteroError,
    ZoteroNotRunning,
    file_url_to_path,
)
from tests.helpers import raw_item, raw_pdf


def client_answering(handler) -> ZoteroClient:
    return ZoteroClient(transport=httpx.MockTransport(handler))


def test_index_reads_items_and_their_export_keys():
    index = LibraryIndex.from_api_items(
        [
            raw_item(
                "ITEM0001",
                "An Empirical Study",
                cite_key="heltweg_empirical_2025",
                doi="10.1000/ABC",
                authors=("Heltweg", "Riehle"),
            ),
            {"key": "NOTE0001", "data": {"key": "NOTE0001", "itemType": "note"}, "bibtex": ""},
        ]
    )
    assert list(index.items) == ["ITEM0001"]
    item = index.items["ITEM0001"]
    assert item.authors == ["Heltweg", "Riehle"]
    assert item.year == "2025"
    assert item.cite_keys == {"heltweg_empirical_2025": KEY_FROM_EXPORT}
    assert index.by_doi["10.1000/abc"] == ["ITEM0001"]
    assert index.by_title["an empirical study"] == ["ITEM0001"]
    assert index.by_cite_key["heltweg_empirical_2025"] == ["ITEM0001"]


def test_index_reads_cite_keys_from_field_and_extra():
    index = LibraryIndex.from_api_items(
        [
            raw_item("ITEM0001", "One", cite_key="one_2025", citation_key_field="pinned1"),
            raw_item(
                "ITEM0002", "Two", cite_key="two_2025", extra="tex.ids: x\nCitation Key: pinned2"
            ),
        ]
    )
    assert index.items["ITEM0001"].cite_keys == {
        "one_2025": KEY_FROM_EXPORT,
        "pinned1": KEY_FROM_FIELD,
    }
    assert index.items["ITEM0002"].cite_keys == {
        "two_2025": KEY_FROM_EXPORT,
        "pinned2": KEY_FROM_EXTRA,
    }
    assert index.by_cite_key["pinned2"] == ["ITEM0002"]


def test_pdf_attachments_are_ordered_by_date_added():
    index = LibraryIndex.from_api_items(
        [
            raw_pdf("PDFLATER", "ITEM0001", date_added="2025-05-01T10:00:00Z"),
            raw_pdf("PDFFIRST", "ITEM0001", date_added="2024-08-12T00:00:08Z"),
            raw_pdf("SNAPSHOT", "ITEM0001", content_type="text/html"),
            raw_pdf("WEBLINK1", "ITEM0001", link_mode="linked_url"),
            raw_pdf("TRASHED1", "ITEM0001", date_added="2020-01-01T00:00:00Z", deleted=True),
            raw_item("ITEM0001", "One", cite_key="one_2025"),
        ]
    )
    assert [pdf.key for pdf in index.items["ITEM0001"].pdfs] == ["PDFFIRST", "PDFLATER"]


def test_trashed_items_are_skipped():
    trashed = raw_item("ITEM0001", "One", cite_key="one_2025")
    trashed["data"]["deleted"] = 1
    assert LibraryIndex.from_api_items([trashed]).items == {}


def test_library_is_read_with_one_get_request():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=[raw_item("ITEM0001", "One", cite_key="one_2025")])

    library = client_answering(handler).load_library()
    assert list(library.items) == ["ITEM0001"]
    assert [(r.method, r.url.path) for r in requests] == [("GET", "/api/users/0/items")]
    assert requests[0].url.params["include"] == "data,bibtex"


def test_403_means_local_api_is_disabled():
    client = client_answering(lambda request: httpx.Response(403, text="Local API is not enabled"))
    with pytest.raises(ZoteroApiDisabled):
        client.load_library()


def test_refused_connection_means_zotero_is_not_running():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused")

    with pytest.raises(ZoteroNotRunning):
        client_answering(handler).load_library()


def test_other_status_is_an_error():
    with pytest.raises(ZoteroError):
        client_answering(lambda request: httpx.Response(500)).load_library()


def test_attachment_file_url():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/users/0/items/PDF00001/file/view/url":
            return httpx.Response(200, text="file:///Users/me/Zotero/storage/PDF00001/A%20B.pdf")
        return httpx.Response(404, text="Not found")

    client = client_answering(handler)
    assert client.attachment_file_url("PDF00001") == (
        "file:///Users/me/Zotero/storage/PDF00001/A%20B.pdf"
    )
    assert client.attachment_file_url("MISSING1") is None


def test_file_url_to_path():
    path = file_url_to_path("file:///Users/me/Zotero/storage/X/Heltweg%20-%202025%20-%20%C3%9C.pdf")
    assert str(path) == "/Users/me/Zotero/storage/X/Heltweg - 2025 - Ü.pdf"
    assert file_url_to_path("https://example.org/a.pdf") is None
    assert file_url_to_path(None) is None
