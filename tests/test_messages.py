from app.agents.messages import Task, Result, ConfirmationRequest, Decision


def test_task_has_id():
    t1 = Task(content="x")
    t2 = Task(content="y")
    assert t1.id != t2.id
    assert t1.content == "x"


def test_result_defaults():
    r = Result(task_id="1", content="ok")
    assert r.success is True


def test_decision_values_match_audit_strings():
    assert Decision.APPROVED.value == "approved"
    assert Decision.REJECTED.value == "rejected"
    assert isinstance(Decision.APPROVED, str)


def test_only_rejection_is_not_approval():
    assert [d for d in Decision if not d.approved] == [Decision.REJECTED]


def test_scope_names_tool_target_and_program():
    req = ConfirmationRequest(run_id="r", agent_id="a", tool_call_id="c", tool_name="ssh_exec",
                              args={"host": "node-a", "command": ["systemctl", "restart", "x"]})
    assert req.scope() == "ssh_exec: host=node-a, program=systemctl"


def _shell_scope(*command):
    return ConfirmationRequest(run_id="r", agent_id="a", tool_call_id="c", tool_name="shell_exec",
                               args={"command": list(command)}).scope()


def test_scope_names_docker_subcommand():
    assert _shell_scope("docker", "restart", "x") == "shell_exec: program=docker restart"
    assert _shell_scope("docker", "compose", "restart") == "shell_exec: program=docker compose restart"


def test_no_scope_for_docker_that_runs_arbitrary_commands():
    # «Да на всё» на `docker exec … psql` не должно открывать `docker exec … sh -c`,
    # `docker run -v /:/host` и `docker rm`.
    assert _shell_scope("docker", "exec", "pg", "psql", "-c", r"\dt") is None
    assert _shell_scope("docker", "run", "-v", "/:/host", "alpine") is None
    assert _shell_scope("docker", "create", "alpine") is None
    assert _shell_scope("docker", "container", "exec", "pg", "sh") is None
    assert _shell_scope("docker", "compose", "exec", "db", "psql") is None
    assert _shell_scope("docker", "compose", "run", "db", "sh") is None
    # Подкоманду за опциями не ищем: кнопки нет.
    assert _shell_scope("docker", "--context", "x", "ps") is None
    assert _shell_scope("docker", "compose", "-f", "x.yml", "up") is None
    assert _shell_scope("docker") is None


def test_no_scope_for_ssh_in_shell():
    assert _shell_scope("ssh", "node-a", "rm", "-rf", "/") is None


def test_confirmation_request_ids_are_separate():
    req = ConfirmationRequest(run_id="r", agent_id="a#1", tool_call_id="c1",
                              tool_name="t", args={})
    assert (req.run_id, req.agent_id, req.tool_call_id) == ("r", "a#1", "c1")
    assert not hasattr(req, "task_id")
