from app.memory.history import DialogHistory


def _store(tmp_path, limit=20):
    return DialogHistory(db_path=str(tmp_path / "dialog.db"), limit=limit)


def test_append_and_load_roundtrip(tmp_path):
    h = _store(tmp_path)
    h.append("user", "привет")
    h.append("assistant", "здравствуй")
    assert h.load() == [
        {"role": "user", "content": "привет"},
        {"role": "assistant", "content": "здравствуй"},
    ]


def test_load_returns_last_n_in_chronological_order(tmp_path):
    h = _store(tmp_path, limit=2)
    for i in range(5):
        h.append("user", f"m{i}")
    assert h.load() == [
        {"role": "user", "content": "m3"},
        {"role": "user", "content": "m4"},
    ]


def test_clear_empties_history(tmp_path):
    h = _store(tmp_path)
    h.append("user", "x")
    h.clear()
    assert h.load() == []


def test_persists_across_instances(tmp_path):
    path = str(tmp_path / "dialog.db")
    DialogHistory(db_path=path, limit=20).append("user", "запомни")
    assert DialogHistory(db_path=path, limit=20).load() == [
        {"role": "user", "content": "запомни"}
    ]
