from app.skills.observe.tools import _is_read_only, observe_query, build_tools
from app.tools.base import Safety


def test_diagnostic_binaries_allowed():
    assert _is_read_only(["free", "-m"])
    assert _is_read_only(["vmstat", "1", "3"])
    assert _is_read_only(["ss", "-tlnp"])
    assert _is_read_only(["ps", "aux", "--sort=-%cpu"])


def test_journalctl_read_vs_mutating():
    assert _is_read_only(["journalctl", "-u", "nginx", "-n", "100", "--no-pager"])
    assert not _is_read_only(["journalctl", "--vacuum-size", "100M"])


def test_systemctl_status_only():
    assert _is_read_only(["systemctl", "status", "docker"])
    assert not _is_read_only(["systemctl", "restart", "nginx"])


def test_log_readers_restricted_to_var_log():
    assert _is_read_only(["tail", "-n", "100", "/var/log/nginx/error.log"])
    assert not _is_read_only(["cat", "/etc/shadow"])
    assert not _is_read_only(["tail", "/opt/secret.env"])


def test_unknown_blocked():
    assert not _is_read_only(["rm", "-rf", "/"])
    assert not _is_read_only([])


async def test_observe_query_rejects_non_readonly():
    out = await observe_query(command=["rm", "-rf", "/"])
    assert "error" in out


async def test_observe_query_runs_via_host_exec(monkeypatch):
    import app.skills.observe.tools as ot

    async def fake_host_exec(command):
        return {"command": command, "returncode": 0, "stdout": "ok", "stderr": ""}

    monkeypatch.setattr(ot, "host_exec", fake_host_exec)
    out = await observe_query(command=["free", "-m"])
    assert out["returncode"] == 0


def test_all_observe_tools_safe():
    for t in build_tools():
        assert t.safety is Safety.SAFE
