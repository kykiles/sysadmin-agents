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


def test_confirmation_request_ids_are_separate():
    req = ConfirmationRequest(run_id="r", agent_id="a#1", tool_call_id="c1",
                              tool_name="t", args={})
    assert (req.run_id, req.agent_id, req.tool_call_id) == ("r", "a#1", "c1")
    assert not hasattr(req, "task_id")
