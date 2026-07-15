from app.bot.render import render_answer, split_message


def test_plain_text_passes_through():
    assert render_answer("Ноды: 10, все на связи") == "Ноды: 10, все на связи"


def test_escapes_raw_html_from_model():
    out = render_answer("<script>alert(1)</script> и A & B")
    assert "<script>" not in out
    assert "&lt;script&gt;" in out
    assert "&amp;" in out


def test_bold_and_code():
    assert render_answer("**Ноды** `1.2.3.4`") == "<b>Ноды</b> <code>1.2.3.4</code>"


def test_quote_lines_merge_into_single_blockquote():
    out = render_answer("Итог\n> node-1: `1.2.3.4`\n> node-2: `5.6.7.8`")
    assert out.count("<blockquote>") == 1
    assert out.count("</blockquote>") == 1
    assert "node-1: <code>1.2.3.4</code>\nnode-2: <code>5.6.7.8</code>" in out


def test_separate_quote_blocks_are_separate():
    out = render_answer("> a\n\nтекст\n\n> b")
    assert out.count("<blockquote>") == 2


def test_heading_becomes_bold():
    assert render_answer("# Отчёт") == "<b>Отчёт</b>"


def test_unclosed_bold_marker_stays_text():
    out = render_answer("**незакрытый жирный")
    assert "<b>" not in out
    assert "**незакрытый жирный" in out


def test_markup_inside_code_is_not_parsed():
    out = render_answer("`ls **/*.py`")
    assert out == "<code>ls **/*.py</code>"


def test_html_inside_code_is_escaped():
    out = render_answer("`cat <file> | grep x`")
    assert out == "<code>cat &lt;file&gt; | grep x</code>"


def test_empty_input():
    assert render_answer("") == ""


def test_quote_marker_needs_no_trailing_space():
    assert "<blockquote>x</blockquote>" in render_answer(">x")


def test_split_short_message_untouched():
    assert split_message("короткий") == ["короткий"]


def test_split_long_message_by_lines():
    text = "\n".join(f"строка {i}" for i in range(1000))
    parts = split_message(text, limit=100)
    assert len(parts) > 1
    assert all(len(render_answer(p)) <= 100 for p in parts)
    assert "".join(p.replace("\n", "") for p in parts) == text.replace("\n", "")


def test_split_single_line_longer_than_limit():
    """Строку без переводов режем принудительно — символы целы, переносы добавляются."""
    parts = split_message("x" * 250, limit=100)
    assert len(parts) > 1
    assert "".join(parts).replace("\n", "") == "x" * 250
    assert all(len(render_answer(p)) <= 100 for p in parts)


def test_escape_heavy_text_stays_under_telegram_limit():
    """& -> &amp; раздувает текст в 5 раз: наивный запас по длине сырья тут не спасает."""
    text = "\n".join("&" * 80 for _ in range(200))
    for part in split_message(text):
        assert len(render_answer(part)) <= 4000


def test_worst_case_escape_stays_under_limit():
    """'\"' -> '&quot;' — шестикратное раздувание, худший случай."""
    for part in split_message('"' * 20000):
        assert len(render_answer(part)) <= 4000


def test_split_preserves_content():
    text = "\n".join(f"нода {i}: `10.0.0.{i}`" for i in range(500))
    assert "\n".join(split_message(text)) == text
