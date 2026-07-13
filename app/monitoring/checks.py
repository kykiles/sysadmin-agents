import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from app.tools.docker import host_exec, docker_ps
from app.skills.security.tools import tls_check
from app.skills.remnawave.tools import _run_script


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str
    value: str = ""


# ---------- pure parsers (легко тестировать) ----------

def _parse_disk_pct(stdout: str) -> float:
    # `df -P /` → строка данных, 5-е поле вида "42%"
    lines = [ln for ln in stdout.splitlines() if ln.strip()]
    parts = lines[-1].split()
    return float(parts[4].rstrip("%"))


def _parse_mem_available_mb(stdout: str) -> int:
    # `free -m` → строка "Mem:", последнее поле = available
    for ln in stdout.splitlines():
        if ln.startswith("Mem:"):
            parts = ln.split()
            return int(parts[6])
    raise ValueError("нет строки Mem: в выводе free")


def _parse_load1(stdout: str) -> float:
    # `uptime` → "... load average: 1.00, 0.80, 0.50"
    m = re.search(r"load average:\s*([\d.]+)", stdout)
    if not m:
        raise ValueError("нет load average в выводе uptime")
    return float(m.group(1))


def _parse_tls_days_left(certificate: str, now: datetime) -> int:
    # строка вида "notAfter=Jul 20 12:00:00 2026 GMT"
    m = re.search(r"notAfter=(.+)", certificate)
    if not m:
        raise ValueError("нет notAfter в сертификате")
    exp = datetime.strptime(m.group(1).strip(), "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
    return (exp - now).days


def _docker_states(ps_result: dict) -> dict[str, str]:
    states: dict[str, str] = {}
    for c in ps_result.get("containers", []):
        state = c.get("State", "")
        for name in c.get("Names", []):
            states[name.lstrip("/")] = state
    return states


def _nodes_status(stdout: str) -> list[dict]:
    data = json.loads(stdout)
    return data if isinstance(data, list) else []


# ---------- проверки ----------

async def check_disk(pct_limit: float) -> CheckResult:
    try:
        res = await host_exec(["df", "-P", "/"])
        used = _parse_disk_pct(res.get("stdout", ""))
    except Exception as e:
        return CheckResult("disk", False, f"проверка не выполнилась: {e}")
    ok = used < pct_limit
    return CheckResult("disk", ok, f"использование / = {used:.0f}% (порог {pct_limit:.0f}%)", f"{used:.0f}%")


async def check_memory(min_mb: int) -> CheckResult:
    try:
        res = await host_exec(["free", "-m"])
        avail = _parse_mem_available_mb(res.get("stdout", ""))
    except Exception as e:
        return CheckResult("memory", False, f"проверка не выполнилась: {e}")
    ok = avail >= min_mb
    return CheckResult("memory", ok, f"доступно {avail} МБ (минимум {min_mb} МБ)", f"{avail}MB")


async def check_load(per_cpu: float) -> CheckResult:
    try:
        res = await host_exec(["uptime"])
        load1 = _parse_load1(res.get("stdout", ""))
    except Exception as e:
        return CheckResult("load", False, f"проверка не выполнилась: {e}")
    cpus = os.cpu_count() or 1
    limit = per_cpu * cpus
    ok = load1 < limit
    return CheckResult("load", ok, f"load1 = {load1:.2f} на {cpus} CPU (порог {limit:.1f})", f"{load1:.2f}")


async def check_docker(containers: list[str]) -> list[CheckResult]:
    try:
        states = _docker_states(await docker_ps())
    except Exception as e:
        return [CheckResult(f"docker:{n}", False, f"проверка не выполнилась: {e}") for n in containers]
    results = []
    for name in containers:
        state = states.get(name)
        if state is None:
            results.append(CheckResult(f"docker:{name}", False, "контейнер не найден"))
        elif state != "running":
            results.append(CheckResult(f"docker:{name}", False, f"состояние: {state}", state))
        else:
            results.append(CheckResult(f"docker:{name}", True, "running", state))
    return results


async def check_rw_nodes() -> CheckResult:
    try:
        res = await _run_script("nodes", [])
        if res.get("returncode") != 0:
            return CheckResult("rw_nodes", False, f"панель недоступна: {res.get('stderr') or res.get('error')}")
        nodes = _nodes_status(res.get("stdout", ""))
    except Exception as e:
        return CheckResult("rw_nodes", False, f"проверка не выполнилась: {e}")
    down = [n for n in nodes if not n.get("isDisabled") and not n.get("isConnected")]
    if down:
        names = ", ".join(n.get("name", "?") for n in down)
        return CheckResult("rw_nodes", False, f"ноды offline: {names}")
    online = sum(n.get("usersOnline") or 0 for n in nodes)
    return CheckResult("rw_nodes", True, f"{len(nodes)} нод подключены, онлайн юзеров: {online}")


async def check_tls(endpoint: str, warn_days: int) -> CheckResult:
    name = f"tls:{endpoint}"
    try:
        res = await tls_check(endpoint)
        cert = res.get("certificate", "")
        if not cert:
            return CheckResult(name, False, f"сертификат не получен: {res.get('stderr')}")
        days = _parse_tls_days_left(cert, datetime.now(timezone.utc))
    except Exception as e:
        return CheckResult(name, False, f"проверка не выполнилась: {e}")
    ok = days >= warn_days
    return CheckResult(name, ok, f"до истечения {days} дней (порог {warn_days})", f"{days}d")
