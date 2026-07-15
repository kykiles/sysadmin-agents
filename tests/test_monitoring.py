from datetime import datetime, timedelta, timezone

import pytest

from app.monitoring import checks as ck
from app.monitoring.checks import (
    CheckResult, check_disk, check_memory, check_load,
    check_docker, check_rw_nodes, check_tls,
    _parse_disk_pct, _parse_mem_available_mb, _parse_load1,
    _parse_tls_days_left, _docker_states, _nodes_status,
)
from app.monitoring.state import MonitorState
from app.monitoring.loop import run_tick, run_checks, MonitorConfig
from app.monitoring.triage import triage


# ---------- pure parsers ----------

def test_parse_disk_pct():
    out = "Filesystem 1024-blocks Used Available Capacity Mounted on\n/dev/sda1 100 42 58 42% /"
    assert _parse_disk_pct(out) == 42.0


def test_parse_mem_available():
    out = ("               total        used        free      shared  buff/cache   available\n"
           "Mem:            7900        3000        1000         200        3900        4500\n"
           "Swap:              0           0           0")
    assert _parse_mem_available_mb(out) == 4500


def test_parse_load1():
    assert _parse_load1(" 12:00:00 up 5 days,  load average: 1.42, 0.80, 0.50") == 1.42


def test_parse_tls_days_left():
    now = datetime(2026, 7, 1, tzinfo=timezone.utc)
    cert = "subject=CN=x\nnotAfter=Jul 20 12:00:00 2026 GMT"
    assert _parse_tls_days_left(cert, now) == 19


def test_docker_states_strips_slash():
    ps = {"containers": [{"Names": ["/remnawave"], "State": "running"},
                         {"Names": ["/glowshine"], "State": "restarting"}]}
    assert _docker_states(ps) == {"remnawave": "running", "glowshine": "restarting"}


def test_nodes_status_parses_json():
    nodes = _nodes_status('[{"name":"de","isConnected":true}]')
    assert nodes[0]["name"] == "de"


# ---------- deterministic checks via monkeypatch ----------

async def _fake_host_exec(stdout):
    async def _f(command):
        return {"stdout": stdout, "returncode": 0}
    return _f


async def test_check_disk_ok_and_fail(monkeypatch):
    monkeypatch.setattr(ck, "host_exec", await _fake_host_exec("h\n/dev/sda1 100 42 58 42% /"))
    assert (await check_disk(90.0)).ok
    monkeypatch.setattr(ck, "host_exec", await _fake_host_exec("h\n/dev/sda1 100 95 5 95% /"))
    assert not (await check_disk(90.0)).ok


async def test_check_memory_boundary(monkeypatch):
    out = "h\nMem: 7900 3000 1000 200 3900 150\nSwap: 0 0 0"
    monkeypatch.setattr(ck, "host_exec", await _fake_host_exec(out))
    r = await check_memory(200)
    assert not r.ok  # 150 < 200


async def test_check_load_uses_cpu_count(monkeypatch):
    monkeypatch.setattr(ck.os, "cpu_count", lambda: 4)
    monkeypatch.setattr(ck, "host_exec", await _fake_host_exec("load average: 7.0, 1, 1"))
    assert (await check_load(2.0)).ok  # 7.0 < 8.0
    monkeypatch.setattr(ck, "host_exec", await _fake_host_exec("load average: 9.0, 1, 1"))
    assert not (await check_load(2.0)).ok


async def test_check_failure_is_not_ok(monkeypatch):
    async def boom(command):
        raise RuntimeError("nsenter недоступен")
    monkeypatch.setattr(ck, "host_exec", boom)
    r = await check_disk(90.0)
    assert not r.ok and "не выполнилась" in r.detail


async def test_check_docker(monkeypatch):
    async def fake_ps():
        return {"containers": [{"Names": ["/up"], "State": "running"},
                               {"Names": ["/bad"], "State": "exited"}]}
    monkeypatch.setattr(ck, "docker_ps", fake_ps)
    results = await check_docker(["up", "bad", "missing"])
    by = {r.name: r for r in results}
    assert by["docker:up"].ok
    assert not by["docker:bad"].ok
    assert not by["docker:missing"].ok and "не найден" in by["docker:missing"].detail


async def test_check_rw_nodes_offline(monkeypatch):
    async def fake_run(script, args):
        return {"returncode": 0, "stdout": '[{"name":"de","isConnected":false,"isDisabled":false}]'}
    monkeypatch.setattr(ck, "_run_script", fake_run)
    r = await check_rw_nodes()
    assert not r.ok and "de" in r.detail


async def test_check_tls_warns(monkeypatch):
    exp = (datetime.now(timezone.utc) + timedelta(days=5)).strftime("%b %d %H:%M:%S %Y GMT")
    async def fake_tls(endpoint):
        return {"certificate": f"notAfter={exp}"}
    monkeypatch.setattr(ck, "tls_check", fake_tls)
    r = await check_tls("h:443", 14)
    assert not r.ok  # 5 < 14


# ---------- state ----------

def test_state_roundtrip(tmp_path):
    st = MonitorState(str(tmp_path / "m.db"))
    assert st.load_prev() == {}
    st.save([CheckResult("disk", True, "ok"), CheckResult("load", False, "high")])
    assert st.load_prev() == {"disk": True, "load": False}
    st.save([CheckResult("load", True, "recovered")])
    assert st.load_prev() == {"disk": True, "load": True}


# ---------- edge detection ----------

class FakeBot:
    def __init__(self):
        self.sent = []
    async def send_message(self, chat_id, text, **kw):
        self.sent.append(text)


class FakeLLM:
    async def chat(self, messages, tools=None):
        from app.llm.client import ChoiceMessage
        return ChoiceMessage(content="триаж: диск заполнен", tool_calls=None)


def _cfg():
    return MonitorConfig(interval=1, disk_pct=90, mem_min_mb=200, load_per_cpu=2.0)


async def test_edge_alert_only_on_transition(tmp_path, monkeypatch):
    async def one_fail(tick, cfg):
        return [CheckResult("disk", False, "95%")]
    monkeypatch.setattr("app.monitoring.loop.run_checks", one_fail)
    st = MonitorState(str(tmp_path / "m.db"))
    bot = FakeBot()
    await run_tick(FakeLLM(), bot, 1, st, _cfg(), 0)  # ok(unknown)->fail => alert
    await run_tick(FakeLLM(), bot, 1, st, _cfg(), 1)  # fail->fail => silent
    assert len(bot.sent) == 1
    assert bot.sent[0].startswith("🔴")


async def test_recovery_message(tmp_path, monkeypatch):
    state_holder = {"ok": False}
    async def toggling(tick, cfg):
        return [CheckResult("disk", state_holder["ok"], "detail")]
    monkeypatch.setattr("app.monitoring.loop.run_checks", toggling)
    st = MonitorState(str(tmp_path / "m.db"))
    bot = FakeBot()
    await run_tick(FakeLLM(), bot, 1, st, _cfg(), 0)  # fail => alert
    state_holder["ok"] = True
    await run_tick(FakeLLM(), bot, 1, st, _cfg(), 1)  # fail->ok => recovered
    assert bot.sent[0].startswith("🔴")
    assert bot.sent[1].startswith("✅ <b>Восстановлено: disk</b>")


async def test_healthy_first_run_is_silent(tmp_path, monkeypatch):
    async def all_ok(tick, cfg):
        return [CheckResult("disk", True, "ok")]
    monkeypatch.setattr("app.monitoring.loop.run_checks", all_ok)
    st = MonitorState(str(tmp_path / "m.db"))
    bot = FakeBot()
    await run_tick(FakeLLM(), bot, 1, st, _cfg(), 0)
    assert bot.sent == []


# ---------- triage fallback ----------

async def test_triage_fallback_when_llm_down():
    class BrokenLLM:
        async def chat(self, messages, tools=None):
            raise RuntimeError("llm down")
    text = await triage(BrokenLLM(), CheckResult("disk", False, "95%"))
    assert text == "disk: 95%"


async def test_triage_uses_llm_content():
    text = await triage(FakeLLM(), CheckResult("disk", False, "95%"))
    assert "триаж" in text


# ---------- run_checks gating ----------

async def test_run_checks_skips_optional_when_unconfigured(monkeypatch):
    async def ok(*a, **k):
        return CheckResult("x", True, "")
    for fn in ("check_disk", "check_memory", "check_load"):
        monkeypatch.setattr(f"app.monitoring.loop.{fn}", ok)
    cfg = MonitorConfig(interval=1, disk_pct=90, mem_min_mb=200, load_per_cpu=2.0)
    results = await run_checks(0, cfg)
    assert len(results) == 3  # без docker/remnawave/tls
