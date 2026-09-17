"""Перенос старых баз (dialog.db + tasks.db) в общую memory.db."""
import sqlite3

from agent_memory.facts import KnowledgeStore
from agent_memory.journal import TaskJournal
from agent_memory.lint import LintState, StaleFact
from app.memory.migrate import migrate_legacy
from datetime import datetime, timedelta, timezone


def _old_dbs(tmp_path):
    """Старые базы в том виде, в каком они лежат на проде."""
    dialog = str(tmp_path / "dialog.db")
    facts = KnowledgeStore(dialog)
    facts.remember("host", "cpu", "2 vCPU", description="железо сервера")
    facts.remember("bot", "path", "/opt/sysadmin-agents")
    facts.propose("web", "asn", "AS1299", run_id="r1", tool="remember_fact", source="ip-api")

    journal = TaskJournal(str(tmp_path / "tasks.db"))
    journal.record(task_id="t1", chat_id="1", intent="перезапусти контейнер xray",
                   agents=["spawned:docker"], tool_seq=["docker_restart"], iterations=3,
                   success=True, summary="контейнер поднят")
    journal.save_transcript("t1", "полный ход задачи", keep=20)
    LintState(str(tmp_path / "tasks.db")).mark_reported(
        [StaleFact(scope="host", key="cpu", value="2 vCPU", kind="stable", age_days=60)],
        datetime.now(timezone.utc),
    )
    return dialog


def _new_stores(tmp_path):
    memory = str(tmp_path / "memory.db")
    return memory, KnowledgeStore(memory), TaskJournal(memory), LintState(memory)


def test_migrates_facts_journal_and_lint(tmp_path):
    dialog = _old_dbs(tmp_path)
    memory, facts, journal, lint = _new_stores(tmp_path)

    moved = migrate_legacy(memory, dialog_db=dialog)

    assert moved == {"facts": 2, "fact_proposals": 1, "tasks": 1,
                     "transcripts": 1, "lint_seen": 1, "tasks_fts": 1}
    assert [(f["scope"], f["key"], f["value"]) for f in facts.recall()] == [
        ("bot", "path", "/opt/sysadmin-agents"),
        ("host", "cpu", "2 vCPU"),
    ]
    assert facts.recall("host")[0]["description"] == "железо сервера"
    assert [p["key"] for p in facts.proposals()] == ["asn"]
    # FTS пересобран — recall_experience находит старую задачу
    assert journal.search("перезапуск контейнера")[0]["summary"] == "контейнер поднят"
    assert journal.transcript() == ("t1", "полный ход задачи")
    since = datetime.now(timezone.utc) - timedelta(days=1)
    assert lint.reported_since(since) == {("host", "cpu")}


def test_second_start_does_not_duplicate(tmp_path):
    dialog = _old_dbs(tmp_path)
    memory, facts, journal, _lint = _new_stores(tmp_path)
    migrate_legacy(memory, dialog_db=dialog)

    assert migrate_legacy(memory, dialog_db=dialog) == {}
    assert len(facts.recall()) == 2
    assert len(journal.search("контейнер")) == 1


def test_missing_old_dbs_are_skipped(tmp_path):
    memory, facts, _journal, _lint = _new_stores(tmp_path)

    assert migrate_legacy(memory, dialog_db=str(tmp_path / "dialog.db")) == {}
    assert facts.recall() == []


def test_facts_of_older_schema_are_copied(tmp_path):
    """В старой базе могло не быть колонок, добавленных `_add_column`."""
    dialog = str(tmp_path / "dialog.db")
    with sqlite3.connect(dialog) as conn:
        conn.execute("CREATE TABLE facts (scope TEXT, key TEXT, value TEXT, ts TEXT)")
        conn.execute("INSERT INTO facts VALUES ('host', 'ip', '1.2.3.4', '2026-01-01T00:00:00')")
    memory, facts, _journal, _lint = _new_stores(tmp_path)

    assert migrate_legacy(memory, dialog_db=dialog) == {"facts": 1}
    assert facts.recall() == [
        {"scope": "host", "key": "ip", "value": "1.2.3.4", "kind": "stable", "description": ""}
    ]
    # у старых фактов одна метка ts вместо периодов — становятся действующими
    assert facts.all_live()[0]["confirmed_at"] == "2026-01-01T00:00:00"


def test_fts_rebuilt_when_tasks_arrived_without_it(tmp_path):
    """Прерванный перенос оставил задачи без поиска — следующий старт дособирает."""
    dialog = _old_dbs(tmp_path)
    memory, _facts, journal, _lint = _new_stores(tmp_path)
    migrate_legacy(memory, dialog_db=dialog)
    with sqlite3.connect(memory) as conn:
        conn.execute("DELETE FROM tasks_fts")
    assert journal.search("контейнер") == []

    assert migrate_legacy(memory, dialog_db=dialog) == {"tasks_fts": 1}
    assert len(journal.search("контейнер")) == 1
