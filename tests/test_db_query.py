from app.skills.db.tools import _is_read_only, docker_query, build_tools
from app.tools.base import Safety


def test_select_allowed():
    assert _is_read_only(["psql", "-U", "u", "-d", "db", "-c", "SELECT count(*) FROM users"])
    assert _is_read_only(["mysql", "-e", "SHOW TABLES"])
    assert _is_read_only(["sqlite3", "/data/app.db", "SELECT 1"])
    assert _is_read_only(["psql", "-c", "\\dt"])


def test_writes_rejected():
    assert not _is_read_only(["psql", "-c", "DELETE FROM users"])
    assert not _is_read_only(["psql", "-c", "SELECT 1; DROP TABLE users"])
    assert not _is_read_only(["mysql", "-e", "UPDATE users SET admin=1"])
    # запись, спрятанная в CTE
    assert not _is_read_only(["psql", "-c", "WITH x AS (INSERT INTO t VALUES (1)) SELECT 1"])


def test_shell_escape_rejected():
    assert not _is_read_only(["psql", "-c", "\\! rm -rf /"])
    assert not _is_read_only(["sqlite3", "/data/app.db", ".shell sh"])
    assert not _is_read_only(["sqlite3", "/data/app.db", ".output /etc/passwd"])


def test_file_execution_rejected():
    assert not _is_read_only(["psql", "-f", "/tmp/anything.sql"])
    assert not _is_read_only(["sqlite3", "-init", "/tmp/x.sql", "/data/app.db"])


def test_non_client_binaries_rejected():
    assert not _is_read_only(["sh", "-c", "rm -rf /"])
    assert not _is_read_only(["cat", "/etc/passwd"])
    assert not _is_read_only([])


def test_interactive_shell_rejected():
    # без запроса клиент открыл бы интерактивную сессию
    assert not _is_read_only(["psql", "-U", "u", "-d", "db"])
    assert not _is_read_only(["psql", "-c"])


async def test_docker_query_rejects_write():
    out = await docker_query(container="pg", command=["psql", "-c", "DROP TABLE users"])
    assert "error" in out


async def test_docker_query_runs_via_docker_exec(monkeypatch):
    import app.skills.db.tools as dt

    async def fake_docker_exec(container, command):
        return {"container": container, "command": command, "output": "1", "exit_code": 0}

    monkeypatch.setattr(dt, "docker_exec", fake_docker_exec)
    out = await docker_query(container="pg", command=["psql", "-c", "SELECT 1"])
    assert out["exit_code"] == 0


def test_tool_stays_safe():
    (tool,) = build_tools()
    assert tool.name == "docker_query"
    assert tool.safety is Safety.SAFE


def test_psql_list_databases_allowed():
    """Без этого агент угадывал имя БД вместо того, чтобы перечислить базы."""
    assert _is_read_only(["psql", "-U", "postgres", "-l"])
    assert _is_read_only(["psql", "-U", "postgres", "--list"])


def test_listing_flag_does_not_smuggle_writes():
    assert not _is_read_only(["psql", "-l", "-c", "DROP TABLE users"])
    assert not _is_read_only(["psql", "-l", "-f", "/tmp/evil.sql"])
    assert not _is_read_only(["mysql", "-l"])
