import sqlite3

from app.memory.facts import KnowledgeStore


def _store(tmp_path):
    return KnowledgeStore(db_path=str(tmp_path / "dialog.db"))


def test_remember_and_recall_roundtrip(tmp_path):
    s = _store(tmp_path)
    s.remember("global", "nginx_conf_path", "/etc/nginx/nginx.conf")
    assert s.recall() == [
        {"scope": "global", "key": "nginx_conf_path", "value": "/etc/nginx/nginx.conf", "kind": "stable"}
    ]


def test_upsert_overwrites_same_scope_key(tmp_path):
    s = _store(tmp_path)
    s.remember("78.17.65.121", "postgres_version", "15")
    s.remember("78.17.65.121", "postgres_version", "16")
    assert s.recall(scope="78.17.65.121") == [
        {"scope": "78.17.65.121", "key": "postgres_version", "value": "16", "kind": "stable"}
    ]


def test_recall_filters_by_scope(tmp_path):
    s = _store(tmp_path)
    s.remember("global", "k", "v1")
    s.remember("host-a", "k", "v2")
    assert s.recall(scope="host-a") == [{"scope": "host-a", "key": "k", "value": "v2", "kind": "stable"}]


def test_recall_filters_by_query(tmp_path):
    s = _store(tmp_path)
    s.remember("global", "postgres_version", "16")
    s.remember("global", "nginx_conf_path", "/etc/nginx")
    assert s.recall(query="postgres") == [
        {"scope": "global", "key": "postgres_version", "value": "16", "kind": "stable"}
    ]


def test_forget_removes_fact(tmp_path):
    s = _store(tmp_path)
    s.remember("global", "k", "v")
    s.forget("global", "k")
    assert s.recall() == []


def test_forget_scope_removes_all_facts_of_scope(tmp_path):
    s = _store(tmp_path)
    s.remember("host-a", "k1", "v1")
    s.remember("host-a", "k2", "v2")
    s.remember("global", "k", "v")
    removed = s.forget_scope("host-a")
    assert removed == 2
    assert s.recall() == [{"scope": "global", "key": "k", "value": "v", "kind": "stable"}]


def test_remember_stores_snapshot_kind(tmp_path):
    s = _store(tmp_path)
    s.remember("host-a", "ssh_port", "2222", kind="snapshot")
    assert s.recall() == [
        {"scope": "host-a", "key": "ssh_port", "value": "2222", "kind": "snapshot"}
    ]


def test_upsert_updates_kind(tmp_path):
    s = _store(tmp_path)
    s.remember("host-a", "ssh_port", "22")
    s.remember("host-a", "ssh_port", "2222", kind="snapshot")
    assert s.recall()[0]["kind"] == "snapshot"


def test_migrates_db_without_kind_column(tmp_path):
    path = str(tmp_path / "facts.db")
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE facts (scope TEXT NOT NULL, key TEXT NOT NULL, "
            "value TEXT NOT NULL, ts TEXT NOT NULL, PRIMARY KEY (scope, key))"
        )
        conn.execute(
            "INSERT INTO facts (scope, key, value, ts) VALUES ('global', 'k', 'v', '2026-07-01')"
        )
    assert KnowledgeStore(db_path=path).recall() == [
        {"scope": "global", "key": "k", "value": "v", "kind": "stable"}
    ]


def test_persists_across_instances(tmp_path):
    path = str(tmp_path / "dialog.db")
    KnowledgeStore(db_path=path).remember("global", "k", "v")
    assert KnowledgeStore(db_path=path).recall() == [
        {"scope": "global", "key": "k", "value": "v", "kind": "stable"}
    ]
