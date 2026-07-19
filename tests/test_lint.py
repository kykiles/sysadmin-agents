from datetime import datetime, timedelta, timezone

from app.learning.lint import LintState, find_stale
from app.memory.facts import KnowledgeStore

NOW = datetime(2026, 7, 19, tzinfo=timezone.utc)
DEFAULTS = dict(stable_days=90, snapshot_days=14, remind_days=30, max_items=10)


def _setup(tmp_path):
    return KnowledgeStore(db_path=str(tmp_path / "facts.db")), LintState(str(tmp_path / "tasks.db"))


def _age(store, scope: str, key: str, days: int) -> None:
    """Отодвигаем ts факта в прошлое — remember() всегда ставит now."""
    ts = (NOW - timedelta(days=days)).isoformat()
    with store._connect() as conn:
        conn.execute("UPDATE facts SET ts = ? WHERE scope = ? AND key = ?", (ts, scope, key))


def test_snapshot_goes_stale_earlier_than_stable(tmp_path):
    store, state = _setup(tmp_path)
    store.remember("host-a", "ssh_port", "2222", kind="snapshot")
    store.remember("host-a", "nginx_conf_path", "/etc/nginx", kind="stable")
    _age(store, "host-a", "ssh_port", 20)
    _age(store, "host-a", "nginx_conf_path", 20)

    found = find_stale(store, state, now=NOW, **DEFAULTS)

    assert [(f.key, f.age_days) for f in found] == [("ssh_port", 20)]


def test_fresh_facts_are_not_reported(tmp_path):
    store, state = _setup(tmp_path)
    store.remember("host-a", "ssh_port", "2222", kind="snapshot")
    _age(store, "host-a", "ssh_port", 5)

    assert find_stale(store, state, now=NOW, **DEFAULTS) == []


def test_stable_fact_goes_stale_after_its_own_ttl(tmp_path):
    store, state = _setup(tmp_path)
    store.remember("host-a", "topology", "single node")
    _age(store, "host-a", "topology", 100)

    found = find_stale(store, state, now=NOW, **DEFAULTS)

    assert [f.key for f in found] == ["topology"]


def test_reported_fact_is_not_repeated_within_remind_window(tmp_path):
    store, state = _setup(tmp_path)
    store.remember("host-a", "ssh_port", "2222", kind="snapshot")
    _age(store, "host-a", "ssh_port", 20)

    first = find_stale(store, state, now=NOW, **DEFAULTS)
    state.mark_reported(first, NOW)

    assert find_stale(store, state, now=NOW + timedelta(days=1), **DEFAULTS) == []


def test_reported_fact_returns_after_remind_window(tmp_path):
    store, state = _setup(tmp_path)
    store.remember("host-a", "ssh_port", "2222", kind="snapshot")
    _age(store, "host-a", "ssh_port", 20)
    state.mark_reported(find_stale(store, state, now=NOW, **DEFAULTS), NOW)

    later = find_stale(store, state, now=NOW + timedelta(days=31), **DEFAULTS)

    assert [f.key for f in later] == ["ssh_port"]


def test_oldest_first_and_capped_by_max_items(tmp_path):
    store, state = _setup(tmp_path)
    for i, days in enumerate([20, 60, 40]):
        store.remember("host-a", f"k{i}", "v", kind="snapshot")
        _age(store, "host-a", f"k{i}", days)

    found = find_stale(store, state, now=NOW, **{**DEFAULTS, "max_items": 2})

    assert [f.age_days for f in found] == [60, 40]


def test_broken_ts_is_skipped_not_crashing(tmp_path):
    store, state = _setup(tmp_path)
    store.remember("host-a", "ssh_port", "2222", kind="snapshot")
    with store._connect() as conn:
        conn.execute("UPDATE facts SET ts = 'not-a-date'")

    assert find_stale(store, state, now=NOW, **DEFAULTS) == []


def test_lint_does_not_modify_facts(tmp_path):
    store, state = _setup(tmp_path)
    store.remember("host-a", "ssh_port", "2222", kind="snapshot")
    _age(store, "host-a", "ssh_port", 20)

    state.mark_reported(find_stale(store, state, now=NOW, **DEFAULTS), NOW)

    assert store.recall() == [
        {"scope": "host-a", "key": "ssh_port", "value": "2222", "kind": "snapshot"}
    ]
