from types import SimpleNamespace
from unittest.mock import AsyncMock

import app.skills.remnawave.tools as rt
from app.tools.base import Safety


async def test_rw_query_rejects_action_script(monkeypatch):
    run = AsyncMock()
    monkeypatch.setattr(rt, "_run_script", run)
    out = await rt.rw_query("user-disable", ["u"])
    assert "error" in out
    run.assert_not_called()


async def test_rw_query_rejects_unknown_and_traversal(monkeypatch):
    run = AsyncMock()
    monkeypatch.setattr(rt, "_run_script", run)
    assert "error" in await rt.rw_query("../_lib", [])
    assert "error" in await rt.rw_query("nope", [])
    run.assert_not_called()


async def test_rw_query_runs_read_script(monkeypatch):
    run = AsyncMock(return_value={"script": "user-find", "returncode": 0})
    monkeypatch.setattr(rt, "_run_script", run)
    out = await rt.rw_query("user-find", ["alice"])
    assert out["returncode"] == 0
    run.assert_awaited_once_with("user-find", ["alice"])


async def test_rw_action_rejects_read_script(monkeypatch):
    run = AsyncMock()
    monkeypatch.setattr(rt, "_run_script", run)
    out = await rt.rw_action("user-get", ["u"])
    assert "error" in out
    run.assert_not_called()


async def test_rw_action_runs_mutation_script(monkeypatch):
    run = AsyncMock(return_value={"script": "user-extend", "returncode": 0})
    monkeypatch.setattr(rt, "_run_script", run)
    out = await rt.rw_action("user-extend", ["u", "30"])
    assert out["returncode"] == 0
    run.assert_awaited_once_with("user-extend", ["u", "30"])


async def test_run_script_injects_env_and_argv(monkeypatch):
    monkeypatch.setattr(rt, "settings", SimpleNamespace(
        remnawave_base_url="https://p.example", remnawave_api_key="secret", remnawave_timeout=30))

    captured = {}

    class FakeProc:
        returncode = 0
        async def communicate(self):
            return (b'{"ok":true}', b"")

    async def fake_exec(*argv, **kwargs):
        captured["argv"] = argv
        captured["env"] = kwargs.get("env")
        return FakeProc()

    monkeypatch.setattr(rt.asyncio, "create_subprocess_exec", fake_exec)
    out = await rt._run_script("user-find", ["alice"])

    assert captured["argv"][0] == "bash"
    assert captured["argv"][1].endswith("scripts/user-find.sh")
    assert captured["argv"][2] == "alice"
    assert captured["env"]["REMNAWAVE_API_KEY"] == "secret"
    assert captured["env"]["REMNAWAVE_BASE_URL"] == "https://p.example"
    assert out["returncode"] == 0
    assert out["stdout"] == '{"ok":true}'


def test_build_tools_safety():
    by_name = {t.name: t for t in rt.build_tools()}
    assert by_name["rw_query"].safety is Safety.SAFE
    assert by_name["rw_action"].safety is Safety.DANGEROUS
