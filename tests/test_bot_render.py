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
    assert text.startswith("Сейчас на этом сервере будет выполнена команда.\n"
                           "Пояснение агента: Освобождаю место — удаляю старые логи.")
    assert "Требуется ваше подтверждение." in text
    assert "инструмент: shell_exec\ncommand: rm -rf /var/log/old" in text
    assert "запрос: RID123" in text


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


# ---------- фраза обычным языком ----------

def _plain_part(text):
    """Всё, что видно без раскрытия свёрнутого блока."""
    return text.split("<blockquote expandable>")[0]


def test_known_tool_is_described_by_code_not_by_model():
    """Живой прогон: модель назвала «пересборкой» вызов compose_up без --build."""
    text = _text(_req("compose_up", reason="Пересоберу образ бота.", project="remnabot", build=False))
    plain = _plain_part(text)
    assert "без пересборки" in plain
    assert "Пересоберу" not in text
    assert "compose_up" not in plain and "project" not in plain


def test_rebuild_phrase():
    plain = _plain_part(_text(_req("compose_up", project="remnabot", build=True)))
    assert plain.startswith("Сейчас будет пересобран образ и перезапущено приложение «remnabot».")


def test_container_restart_phrase_and_details_collapsed():
    text = _text(_req("docker_restart", container="glowshine_bot"))
    assert _plain_part(text) == ("Сейчас будет перезапущен Docker-контейнер «glowshine_bot».\n\n"
                                 "Требуется ваше подтверждение.\n\n")
    assert "Подробности для проверки" in text and "инструмент: docker_restart" in text


def test_rw_action_phrase():
    plain = _plain_part(_text(_req("rw_action", script="user-extend", args=["42", "30"])))
    assert "продлена подписка пользователя «42» на 30 дн." in plain


def test_unknown_tool_falls_back_to_name_and_model_reason():
    plain = _plain_part(_text(_req("mcp_create_issue", reason="Заведу задачу в трекере.", title="x")))
    assert "«mcp_create_issue»" in plain
    assert "Пояснение агента: Заведу задачу в трекере." in plain


def test_malformed_args_do_not_break_phrase():
    plain = _plain_part(_text(_req("docker_restart")))
    assert "«docker_restart»" in plain
