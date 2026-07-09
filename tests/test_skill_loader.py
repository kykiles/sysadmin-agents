from app.skills.loader import parse_frontmatter


def test_parse_frontmatter_extracts_meta_and_body():
    text = "---\nname: docker\ndescription: управление docker\n---\nТело плейбука\n"
    meta, body = parse_frontmatter(text)
    assert meta == {"name": "docker", "description": "управление docker"}
    assert body.strip() == "Тело плейбука"


def test_parse_frontmatter_no_frontmatter_returns_empty_meta():
    meta, body = parse_frontmatter("просто текст")
    assert meta == {}
    assert body == "просто текст"
