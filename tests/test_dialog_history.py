import sqlite3
from datetime import datetime, timedelta, timezone

from app.memory.history import DialogHistory


def _store(tmp_path, limit=20):
    return DialogHistory(db_path=str(tmp_path / "dialog.db"), limit=limit)


def test_append_and_load_roundtrip(tmp_path):
    h = _store(tmp_path)
    h.append("c1", "user", "привет")
    h.append("c1", "assistant", "здравствуй")
    assert h.load("c1") == [
        {"role": "user", "content": "привет"},
        {"role": "assistant", "content": "здравствуй"},
    ]


def test_load_returns_last_n_in_chronological_order(tmp_path):
    h = _store(tmp_path, limit=2)
    for i in range(5):
        h.append("c1", "user", f"m{i}")
    assert h.load("c1") == [
        {"role": "user", "content": "m3"},
        {"role": "user", "content": "m4"},
    ]


def test_clear_empties_history(tmp_path):
    h = _store(tmp_path)
    h.append("c1", "user", "x")
    h.clear("c1")
    assert h.load("c1") == []


def test_persists_across_instances(tmp_path):
    path = str(tmp_path / "dialog.db")
    DialogHistory(db_path=path, limit=20).append("c1", "user", "запомни")
    assert DialogHistory(db_path=path, limit=20).load("c1") == [
        {"role": "user", "content": "запомни"}
    ]


def test_token_budget_truncates_oldest(tmp_path):
    # бюджет ~15 токенов; каждое сообщение ~ len//4+4
    h = DialogHistory(db_path=str(tmp_path / "dialog.db"), limit=100, token_budget=15)
    for i in range(10):
        h.append("c1", "user", "x" * 20)  # ~ 20//4+4 = 9 токенов each
    loaded = h.load("c1")
    # укладываются 1-2 последних сообщения, не все 10
    assert 0 < len(loaded) < 10
    assert loaded[-1]["content"] == "x" * 20


def test_token_budget_keeps_at_least_one(tmp_path):
    h = DialogHistory(db_path=str(tmp_path / "dialog.db"), limit=100, token_budget=1)
    h.append("c1", "user", "y" * 400)
    assert h.load("c1") == [{"role": "user", "content": "y" * 400}]


def test_append_purges_entries_older_than_retention(tmp_path):
    path = str(tmp_path / "dialog.db")
    h = DialogHistory(db_path=path, limit=100, retention_days=30)
    old_ts = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
    with sqlite3.connect(path) as conn:
        conn.execute(
            "INSERT INTO messages (chat_id, role, content, ts) VALUES (?, ?, ?, ?)",
            ("c1", "user", "древнее", old_ts),
        )
    h.append("c1", "user", "свежее")
    assert h.load("c1") == [{"role": "user", "content": "свежее"}]


def test_retention_zero_keeps_everything(tmp_path):
    path = str(tmp_path / "dialog.db")
    h = DialogHistory(db_path=path, limit=100, retention_days=0)
    old_ts = (datetime.now(timezone.utc) - timedelta(days=999)).isoformat()
    with sqlite3.connect(path) as conn:
        conn.execute(
            "INSERT INTO messages (chat_id, role, content, ts) VALUES (?, ?, ?, ?)",
            ("c1", "user", "древнее", old_ts),
        )
    h.append("c1", "user", "свежее")
    assert len(h.load("c1")) == 2


def test_chat_ids_are_isolated(tmp_path):
    h = _store(tmp_path)
    h.append("c1", "user", "секрет-1")
    h.append("c2", "user", "секрет-2")
    assert h.load("c1") == [{"role": "user", "content": "секрет-1"}]
    assert h.load("c2") == [{"role": "user", "content": "секрет-2"}]
    h.clear("c1")
    assert h.load("c1") == []
    assert h.load("c2") == [{"role": "user", "content": "секрет-2"}]
