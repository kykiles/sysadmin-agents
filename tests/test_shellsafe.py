from app.skills.shellsafe import (
    unwrap_shell,
    split_simple_commands,
    check_wrapped_readonly,
)


def test_unwrap_shell():
    assert unwrap_shell(["sh", "-c", "df -h"]) == "df -h"
    assert unwrap_shell(["bash", "-lc", "ls"]) == "ls"
    assert unwrap_shell(["df", "-h"]) is None
    assert unwrap_shell(["bash", "-c"]) is None


def test_split_pipes_and_operators():
    assert split_simple_commands("a b | c d && e") == [["a", "b"], ["c", "d"], ["e"]]


def test_split_for_loop_drops_header():
    cmds = split_simple_commands("for x in /a/*/; do openssl x509 -in x; done")
    # заголовок `for x in /a/*/` отброшен, осталась команда тела
    assert cmds == [["openssl", "x509", "-in", "x"]]


def test_split_inline_redirection_to_null_ok():
    assert split_simple_commands("ls -la 2>/dev/null") == [["ls", "-la"]]
    assert split_simple_commands("cmd 2>&1") == [["cmd"]]


def test_split_write_redirection_rejected():
    assert split_simple_commands("ls > /etc/out") is None
    assert split_simple_commands("ls >> file.txt") is None


def test_split_command_substitution_rejected():
    assert split_simple_commands("echo $(rm -rf /)") is None
    assert split_simple_commands("echo `whoami`") is None


def test_split_syntax_error_rejected():
    assert split_simple_commands("echo 'unterminated") is None


def test_check_wrapped_non_wrapper_returns_none():
    assert check_wrapped_readonly(["df", "-h"], lambda c: True) is None


def test_check_wrapped_builtins_and_filters_autopass():
    # echo/grep/head проходят без обращения к is_simple_readonly
    ok = check_wrapped_readonly(
        ["sh", "-c", "echo hi | grep h | head -1"], lambda c: False
    )
    assert ok is True


def test_check_wrapped_delegates_to_simple():
    seen = []

    def simple(c):
        seen.append(c)
        return c[0] == "openssl"

    assert check_wrapped_readonly(["sh", "-c", "openssl x509"], simple) is True
    assert check_wrapped_readonly(["sh", "-c", "certbot renew"], simple) is False
