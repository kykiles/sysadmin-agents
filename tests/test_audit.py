import json
from app import audit


def test_outcome_extracts_returncode():
    out = audit.outcome(json.dumps({"returncode": 3, "stdout": "boom"}))
    assert out["returncode"] == 3
    assert "boom" in out["preview"]


def test_outcome_handles_non_dict_and_plain():
    assert audit.outcome("plain text")["returncode"] is None
    assert audit.outcome("[1, 2]")["returncode"] is None


async def test_record_appends_jsonl(tmp_path, monkeypatch):
    path = tmp_path / "sub" / "audit.jsonl"
    monkeypatch.setattr(audit.settings, "audit_trail_path", str(path))
    await audit.record(agent="hostadmin", tool="shell_exec",
                       args={"command": ["rm", "-rf", "/x"]}, decision="approved",
                       result={"returncode": 0, "preview": "ok"})
    await audit.record(agent="hostadmin", tool="shell_exec",
                       args={"command": ["reboot"]}, decision="rejected",
                       result={"returncode": None, "preview": ""})
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["agent"] == "hostadmin"
    assert first["decision"] == "approved"
    assert first["tool"] == "shell_exec"
    assert "ts" in first


async def test_record_has_no_setting_secret(tmp_path, monkeypatch):
    path = tmp_path / "audit.jsonl"
    monkeypatch.setattr(audit.settings, "audit_trail_path", str(path))
    monkeypatch.setattr(audit.settings, "remnawave_api_key", "AUDIT_FAKE_SECRET")
    await audit.record(agent="a", tool="rw_curl_write",
                       args={"body": {"nested": ["AUDIT_FAKE_SECRET"]}}, decision="approved",
                       result={"returncode": 0, "preview": "echo AUDIT_FAKE_SECRET"})
    text = path.read_text(encoding="utf-8")
    assert "AUDIT_FAKE_SECRET" not in text
    assert json.loads(text)["tool"] == "rw_curl_write"


async def test_record_swallows_write_errors(monkeypatch):
    def boom(_event):
        raise OSError("disk full")

    monkeypatch.setattr(audit, "_record_sync", boom)
    # не должно бросить исключение
    await audit.record(agent="a", tool="t", args={}, decision="approved", result={})


def test_outcome_keeps_full_size_when_preview_is_cut():
    """Из превью не видно, вернулись три строки или три мегабайта."""
    out = audit.outcome("x" * 5000, limit=300)
    assert out["bytes"] == 5000
    assert len(out["preview"]) == 300


async def test_rotation_keeps_two_generations(tmp_path, monkeypatch):
    """Полный след — это 400+ строк в активный день: без предела файл съест диск."""
    path = tmp_path / "audit.jsonl"
    monkeypatch.setattr(audit.settings, "audit_trail_path", str(path))
    monkeypatch.setattr(audit, "_MAX_BYTES", 200)
    for i in range(20):
        await audit.record(agent="a", tool=f"t{i}", args={"x": "y" * 50},
                           decision="auto", result={"preview": ""})
    assert path.stat().st_size < 400
    assert (tmp_path / "audit.jsonl.1").exists()
    # последняя запись — в текущем файле, ничего не потеряно
    assert json.loads(path.read_text(encoding="utf-8").splitlines()[-1])["tool"] == "t19"
