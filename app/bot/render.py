import html

from app.agents.messages import ConfirmationRequest


def _format_command(tool_name: str, args: dict) -> str:
    command = args.get("command")
    if isinstance(command, list):
        return " ".join(str(a) for a in command)
    if command:
        return str(command)
    if args:
        parts = " ".join(f"{k}={v}" for k, v in args.items())
        return f"{tool_name} {parts}"
    return tool_name


def format_confirmation(req: ConfirmationRequest) -> str:
    lines = ["⚠️ <b>Требуется подтверждение</b>"]
    reason = (req.reason or "").strip()
    if reason:
        lines.append(html.escape(reason))
    command = html.escape(_format_command(req.tool_name, req.args))
    lines.append(f"<blockquote expandable>{command}</blockquote>")
    return "\n\n".join(lines)
