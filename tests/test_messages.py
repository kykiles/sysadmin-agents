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
    assert Decision.AUTO_APPROVED.value == "auto-approved"
    assert isinstance(Decision.APPROVED, str)
