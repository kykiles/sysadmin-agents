"""docker_exec должен вычитывать stream до конца, а не первый фрейм."""
import app.tools.docker as d


class _Msg:
    def __init__(self, data): self.data = data


class _Stream:
    def __init__(self, frames): self._frames = list(frames)

    async def read_out(self):
        return _Msg(self._frames.pop(0)) if self._frames else None


class _Exec:
    def start(self, detach=False): return _Stream([b"psql: error: ", b"connection refused\n"])
    async def inspect(self): return {"ExitCode": 2}


class _Container:
    async def exec(self, cmd): return _Exec()


class _Docker:
    class containers:
        @staticmethod
        def container(name): return _Container()

    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False


async def test_reads_all_frames(monkeypatch):
    monkeypatch.setattr(d, "Docker", lambda: _Docker())
    out = await d.docker_exec("pg", ["psql", "-c", "SELECT 1"])
    assert out["output"] == "psql: error: connection refused\n"
    assert out["exit_code"] == 2
