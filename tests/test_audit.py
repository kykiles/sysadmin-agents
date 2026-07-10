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


async def test_record_swallows_write_errors(monkeypatch):
    def boom(_event):
        raise OSError("disk full")

    monkeypatch.setattr(audit, "_record_sync", boom)
    # не должно бросить исключение
    await audit.record(agent="a", tool="t", args={}, decision="approved", result={})
