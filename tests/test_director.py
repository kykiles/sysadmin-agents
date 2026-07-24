from app.agents.director import Director
from app.agents.registry import AgentRegistry


def test_director_wires_memory():
    class DummyMem:
        def load(self): return []
        def append(self, r, c): ...

    mem = DummyMem()
    reg = AgentRegistry()
    d = Director(llm=None, registry=reg, memory=mem)
    assert d._memory is mem
