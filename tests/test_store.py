from loccit.store import Store


def test_document_of_the_same_name_is_replaced(tmp_path):
    store = Store(tmp_path / "loccit.sqlite")
    first = store.save_document("thesis.pdf", "hash1", has_bib=True)
    other = store.save_document("proposal.pdf", "hash2", has_bib=False)
    again = store.save_document("thesis.pdf", "hash3", has_bib=True)
    assert again.id == first.id != other.id
    assert again.pdf_sha256 == "hash3"
    assert {document.name for document in store.documents()} == {"thesis.pdf", "proposal.pdf"}
    assert store.document(other.id) == other
    assert store.document(999) is None


def test_documents_are_listed_most_recently_opened_first(tmp_path, monkeypatch):
    clock = iter(f"2026-11-17T10:00:0{n}+00:00" for n in range(9))
    monkeypatch.setattr("loccit.store._now", lambda: next(clock))
    store = Store(tmp_path / "loccit.sqlite")
    first = store.save_document("a.pdf", "h", has_bib=False)
    second = store.save_document("b.pdf", "h", has_bib=False)
    assert [d.name for d in store.documents()] == ["b.pdf", "a.pdf"]
    store.touch_document(first.id)
    assert [d.name for d in store.documents()] == ["a.pdf", "b.pdf"]
    store.delete_document(first.id)
    assert [d.id for d in store.documents()] == [second.id]


def test_choices_survive_reopening_the_file(tmp_path):
    store = Store(tmp_path / "loccit.sqlite")
    store.set_item_choice("brown_language_2020", "K38PK3JU")
    store.set_item_choice("brown_language_2020", "SFTVNYYH")
    store.set_pdf_choice("ITEM0001", "PDF00002")
    reopened = Store(tmp_path / "loccit.sqlite")
    assert reopened.item_choices() == {"brown_language_2020": "SFTVNYYH"}
    assert reopened.pdf_choices() == {"ITEM0001": "PDF00002"}
    reopened.set_item_choice("brown_language_2020", None)
    reopened.set_pdf_choice("ITEM0001", None)
    assert reopened.item_choices() == {}
    assert reopened.pdf_choices() == {}
