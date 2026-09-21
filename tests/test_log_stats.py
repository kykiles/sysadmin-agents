"""log_stats — счёт по логу вне модели.

Разбор живого журнала 21.09.2026: задача «откуда приходят на сайт» стоила
109 вызовов `host_query` и 14 минут, потому что распределение по 3142 строкам
агент считал перебором grep, а окно по unix-времени подбирал регулярным
выражением. Инструмент закрывает и то и другое.
"""
import pytest

import skills.observe.tools as st
from app.tools.base import Safety


LOG = "\n".join([
    '{"ts":1789917000.1,"status":200,"req":{"headers":{"Referer":["https://www.threads.net/x"]}}}',
    '{"ts":1789918000.2,"status":200,"req":{"headers":{"Referer":["https://yandex.ru/search"]}}}',
    '{"ts":1789919000.3,"status":404,"req":{"headers":{"Referer":["https://www.threads.net/y"]}}}',
    '{"ts":1790100000.4,"status":200,"req":{"headers":{"Referer":["https://google.com/"]}}}',
])


@pytest.fixture
def host(monkeypatch):
    """Подменяет хост: stat отдаёт размер, cat — содержимое лога."""
    calls = []

    async def fake_host_exec(command):
        calls.append(command)
        if command[0] == "stat":
            return {"returncode": 0, "stdout": f"{len(LOG)}\n", "stderr": ""}
        return {"returncode": 0, "stdout": LOG, "stderr": ""}

    monkeypatch.setattr(st, "host_exec", fake_host_exec)
    return calls


async def test_counts_distribution_by_group(host):
    out = await st.log_stats(paths=["/var/log/cabinet.log"], extract=r'"status":([0-9]+)')
    assert out["lines"] == 4
    assert out["extracted"] == 4
    assert out["top"] == [("200", 3), ("404", 1)]
    assert out["distinct"] == 2


async def test_counts_whole_match_without_group(host):
    out = await st.log_stats(paths=["/var/log/cabinet.log"], extract=r'"status":[0-9]+')
    assert out["top"] == [('"status":200', 3), ('"status":404', 1)]


async def test_where_filters_lines(host):
    out = await st.log_stats(paths=["/var/log/cabinet.log"],
                             extract=r'Referer":\["https?://([^/"]+)',
                             where=r'"status":200')
    assert out["matched"] == 3
    assert out["top"] == [("www.threads.net", 1), ("yandex.ru", 1), ("google.com", 1)]


async def test_window_keeps_only_lines_inside_range(host):
    """Числовой диапазон — то, что шаблоном не выражается."""
    out = await st.log_stats(
        paths=["/var/log/cabinet.log"],
        extract=r'Referer":\["https?://([^/"]+)',
        window={"pattern": r'"ts":([0-9.]+)', "min": 1789917900, "max": 1789919500},
    )
    assert out["matched"] == 2
    assert out["top"] == [("yandex.ru", 1), ("www.threads.net", 1)]


async def test_top_limits_and_counts_the_rest(host):
    out = await st.log_stats(paths=["/var/log/cabinet.log"],
                             extract=r'"status":([0-9]+)', top=1)
    assert out["top"] == [("200", 3)]
    assert out["other"] == 1


async def test_sample_and_note_when_pattern_misses(host):
    """Шаблон не сошёлся — в ответе живая строка, а не пустота: чинить по ней."""
    out = await st.log_stats(paths=["/var/log/cabinet.log"], extract=r'"code":([0-9]+)')
    assert out["extracted"] == 0
    assert out["sample"].startswith('{"ts":1789917000')
    assert "sample" in out["note"]


async def test_sample_when_filter_matches_nothing(host):
    out = await st.log_stats(paths=["/var/log/cabinet.log"],
                             extract=r'"status":([0-9]+)', where="нет такого")
    assert out["matched"] == 0
    assert out["sample"].startswith('{"ts":1789917000')
    assert "первая строка файла" in out["note"]


async def test_reads_several_files_in_one_call(host):
    """Окно задевает текущий файл и ротированный — счёт общий, вызов один."""
    await st.log_stats(paths=["/var/log/cabinet.log", "/var/log/cabinet.log.1"],
                       extract=r'"status":([0-9]+)')
    assert host[-1] == ["cat", "/var/log/cabinet.log", "/var/log/cabinet.log.1"]


async def test_refuses_secret_file(host):
    out = await st.log_stats(paths=["/opt/remnawave/.env"], extract=".")
    assert "error" in out
    assert not host, "секретный файл не должен доходить до хоста"


async def test_refuses_path_outside_read_only_classification(host):
    """Доступ тот же, что у host_query: решает общая классификация, не навык."""
    out = await st.log_stats(paths=["/root/.ssh/id_ed25519"], extract=".")
    assert "error" in out
    assert not host


async def test_bad_regex_returns_reason(host):
    out = await st.log_stats(paths=["/var/log/cabinet.log"], extract="[unclosed")
    assert "неверный regex" in out["error"]


async def test_window_pattern_needs_exactly_one_group(host):
    out = await st.log_stats(paths=["/var/log/cabinet.log"], extract=".",
                             window={"pattern": r'"ts":[0-9.]+', "min": 0, "max": 1})
    assert "одну группу" in out["error"]


async def test_missing_file_reports_stderr(monkeypatch):
    async def fake_host_exec(command):
        return {"returncode": 1, "stdout": "", "stderr": "stat: cannot stat '/var/log/nope': No such file"}

    monkeypatch.setattr(st, "host_exec", fake_host_exec)
    out = await st.log_stats(paths=["/var/log/nope"], extract=".")
    assert "No such file" in out["error"]


async def test_refuses_file_over_the_cap(monkeypatch):
    async def fake_host_exec(command):
        assert command[0] == "stat", "до чтения гигантского файла дело доходить не должно"
        return {"returncode": 0, "stdout": str(st._MAX_BYTES + 1), "stderr": ""}

    monkeypatch.setattr(st, "host_exec", fake_host_exec)
    out = await st.log_stats(paths=["/var/log/huge.log"], extract=".")
    assert "МБ" in out["error"]


def test_log_stats_is_safe_and_declared():
    tool = next(t for t in st.build_tools() if t.name == "log_stats")
    assert tool.safety is Safety.SAFE
