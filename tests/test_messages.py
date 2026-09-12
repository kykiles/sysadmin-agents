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


def test_no_scoped_auto_approval():
    """«Не спрашивать снова» убрано: каждое изменение согласуется отдельно (аудит F04)."""
    assert {d.value for d in Decision} == {"approved", "rejected"}


def test_confirmation_request_ids_are_separate():
    req = ConfirmationRequest(run_id="r", agent_id="a#1", tool_call_id="c1",
                              tool_name="t", args={})
    assert (req.run_id, req.agent_id, req.tool_call_id) == ("r", "a#1", "c1")
    assert not hasattr(req, "task_id")
