from app.agents.messages import ConfirmationRequest
from app.bot.render import confirmation_details, format_confirmation


def _req(tool="shell_exec", reason="", **args):
    return ConfirmationRequest(run_id="r", agent_id="a#1", tool_call_id="c1",
                               tool_name=tool, args=args, reason=reason)


def _text(req, rid="RID123"):
    return format_confirmation(req, rid, confirmation_details(req))


def test_shell_command_rendered_in_blockquote():
    req = _req(command=["rm", "-rf", "/var/log/old"], reason="Освобождаю место — удаляю старые логи.")
    text = _text(req)
    assert "<b>Требуется подтверждение</b>" in text
    assert "Освобождаю место — удаляю старые логи." in text
    assert "инструмент: shell_exec\ncommand: rm -rf /var/log/old" in text
    assert "<code>RID123</code>" in text


def test_every_argument_is_shown_without_reason():
    text = _text(_req("docker_restart", container="bot"))
    assert "container: &quot;bot&quot;" in text
    assert "инструмент: docker_restart" in text


def test_different_hosts_are_distinguishable():
    """Аудит F05: для node-a и node-b подтверждения совпадали побайтно."""
    a = _req("ssh_exec", host="node-a", command=["systemctl", "restart", "nginx"])
    b = _req("ssh_exec", host="node-b", command=["systemctl", "restart", "nginx"])
    assert _text(a) != _text(b)
    assert 'host: "node-a"' in confirmation_details(a)
    assert 'host: "node-b"' in confirmation_details(b)


def test_different_containers_are_distinguishable():
    a = _text(_req("docker_exec", container="pg-prod", command=["psql", "-c", "SELECT 1"]))
    b = _text(_req("docker_exec", container="pg-test", command=["psql", "-c", "SELECT 1"]))
    assert a != b


def test_target_goes_first():
    details = confirmation_details(_req("ssh_exec", command=["uptime"], host="node-a"))
    assert details.splitlines()[:2] == ["инструмент: ssh_exec", 'host: "node-a"']


def test_argv_boundaries_are_visible():
    one = confirmation_details(_req(command=["sh", "-c", "echo a b"]))
    two = confirmation_details(_req(command=["sh", "-c", "echo", "a", "b"]))
    assert one != two


def test_html_is_escaped():
    text = _text(_req(command=["echo", "<b>&"], reason="a < b & c"))
    assert "&lt;b&gt;&amp;" in text
    assert "a &lt; b &amp; c" in text


def test_secret_in_args_is_not_shown(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "remnawave_api_key", "AUDIT_FAKE_SECRET")
    text = _text(_req("rw_curl_write", body={"token": "AUDIT_FAKE_SECRET"}, method="POST", path="/x"))
    assert "AUDIT_FAKE_SECRET" not in text
    assert "&lt;redacted&gt;" in text
