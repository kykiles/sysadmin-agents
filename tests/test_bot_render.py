from app.agents.messages import ConfirmationRequest
from app.bot.render import format_confirmation


def test_shell_command_rendered_in_blockquote():
    req = ConfirmationRequest(
        task_id="x", tool_name="shell_exec",
        args={"command": ["rm", "-rf", "/var/log/old"]},
        description="d", reason="Освобождаю место — удаляю старые логи.",
    )
    text = format_confirmation(req)
    assert "⚠️ <b>Требуется подтверждение</b>" in text
    assert "Освобождаю место — удаляю старые логи." in text
    assert "<blockquote expandable>rm -rf /var/log/old</blockquote>" in text


def test_reason_omitted_when_empty():
    req = ConfirmationRequest(
        task_id="x", tool_name="docker_restart",
        args={"container": "bot"}, description="d",
    )
    text = format_confirmation(req)
    assert "<blockquote expandable>docker_restart container=bot</blockquote>" in text
    # без reason — только заголовок и команда
    assert text.count("\n\n") == 1


def test_html_is_escaped():
    req = ConfirmationRequest(
        task_id="x", tool_name="shell_exec",
        args={"command": ["echo", "<b>&"]}, description="d",
        reason="a < b & c",
    )
    text = format_confirmation(req)
    assert "&lt;b&gt;&amp;" in text
    assert "a &lt; b &amp; c" in text
