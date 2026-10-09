"""The periodic pid sweep and a backend left by a gateway that crashed (D66).

A child Python process stands in for the gateway: it spawns a fake ``kiro-cli``
the way ``acp/launch.py`` does (``start_new_session=True``, the
``KIROCREW_SPAWNED`` marker, pipes) and records it with the real
``_track_session_pid``. The fake is this interpreter reached through a symlink
named ``kiro-cli``, appending a line to a file every 0.1 s. The test process plays
the NEXT gateway: it runs the real boot reap and the real two phases of the
periodic sweep (``_periodic_pid_sweep``, then ``_kill_confirmed_and_writeback``).

Every path is under ``tmp_path`` (``KIROCREW_HOME`` and ``session_pid.config_dir``).
The boot reap's host-wide MCP sweep and its two prunes are replaced by recorders.
Only PIDs a test started are signalled, by recorded PID, and teardown checks that
every one of them is gone.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

import kiro_crew.session_pid as session_pid

pytestmark = pytest.mark.skipif(not Path("/proc/self/stat").exists(), reason="needs /proc")

_FAKE_TURN = (
    "import sys, time\n"
    "while True:\n"
    "    open(sys.argv[1], 'a').write('tick\\n')\n"
    "    time.sleep(0.1)\n"
)

_GATEWAY = textwrap.dedent("""
    import asyncio, json, os, sys
    from kiro_crew.session_pid import _pid_start_token, _track_session_pid
    fake, code, beats = sys.argv[1], sys.argv[2], sys.argv[3]

    async def main():
        env = {**os.environ, "KIROCREW_SPAWNED": "1"}
        proc = await asyncio.create_subprocess_exec(
            fake, "-c", code, beats,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, env=env, start_new_session=True,
            cwd=os.path.dirname(beats),
        )
        _track_session_pid(proc.pid, _pid_start_token(proc.pid))
        print(json.dumps({"gw": os.getpid(), "cli": proc.pid}), flush=True)
        await asyncio.sleep(3600)

    asyncio.run(main())
    """)


def _alive(pid: int) -> bool:
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except OSError:
        return False
    return state != "Z"


def _wait_gone(pid: int, ceiling: float = 5.0) -> bool:
    deadline = time.monotonic() + ceiling
    while _alive(pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    return not _alive(pid)


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    monkeypatch.setattr(session_pid, "config_dir", lambda: home)
    return home


class _Started:
    """Processes this test started, each with the start token read when it was recorded."""

    def __init__(self) -> None:
        self.records: list[tuple[int, str]] = []
        self.popens: list[subprocess.Popen] = []

    def add(self, pid: int) -> None:
        token = session_pid._pid_start_token(pid)
        assert token is not None, f"no start token for pid {pid}: teardown could not identify it"
        self.records.append((pid, token))

    @staticmethod
    def still_running(pid: int, token: str) -> bool:
        """Whether *pid* still names the process recorded with *token*."""
        return _alive(pid) and session_pid._pid_start_token(pid) == token


@pytest.fixture
def started():
    """Processes this test started.

    Teardown signals each one only while its PID still names it (the same
    ``_pid_start_token`` identity the sweep uses), so a PID the system has handed to
    another process since is never signalled. It then checks every one is gone.
    """
    tracked = _Started()
    yield tracked
    for pid, token in tracked.records:
        if tracked.still_running(pid, token):
            os.kill(pid, signal.SIGKILL)
    for proc in tracked.popens:
        proc.wait(timeout=30)
    left = list(tracked.records)
    deadline = time.monotonic() + 5
    while left and time.monotonic() < deadline:
        left = [(pid, token) for pid, token in left if tracked.still_running(pid, token)]
        if left:
            time.sleep(0.05)
    assert not left, f"processes this test started are still running: {left}"


@pytest.fixture
def fake_cli(tmp_path) -> Path:
    (tmp_path / "bin").mkdir()
    fake = tmp_path / "bin" / "kiro-cli"
    fake.symlink_to(sys.executable)
    return fake


def _spawn_gateway(
    home: Path, fake: Path, beats: Path, started: _Started
) -> tuple[subprocess.Popen, int]:
    env = {**os.environ, "KIROCREW_HOME": str(home), "PYTHONDONTWRITEBYTECODE": "1"}
    gw = subprocess.Popen(
        [sys.executable, "-c", _GATEWAY, str(fake), _FAKE_TURN, str(beats)],
        env=env,
        cwd=str(home.parent),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    started.popens.append(gw)
    started.add(gw.pid)
    line = gw.stdout.readline()
    assert line, gw.stderr.read()[-2000:]
    cli = json.loads(line)["cli"]
    started.add(cli)
    return gw, cli


def _crash(gw: subprocess.Popen) -> None:
    os.kill(gw.pid, signal.SIGKILL)
    gw.wait(timeout=30)


def _age_past_grace(monkeypatch, pid: int) -> None:
    real_age = session_pid._pid_age_seconds

    def aged(candidate: int, *args, **kwargs):
        if candidate == pid:
            return session_pid.SWEEP_SPAWN_GRACE_SECONDS + 1
        return real_age(candidate, *args, **kwargs)

    monkeypatch.setattr(session_pid, "_pid_age_seconds", aged)


def _stub_boot_extras(monkeypatch) -> list[str]:
    calls: list[str] = []
    monkeypatch.setattr(
        session_pid, "_cleanup_orphaned_mcp_servers", lambda: calls.append("mcp") or 0
    )
    monkeypatch.setattr(
        session_pid, "_prune_stale_session_pid_files", lambda **kw: calls.append("pid-files") or 0
    )
    monkeypatch.setattr(
        session_pid,
        "_prune_stale_session_token_files",
        lambda *a, **kw: calls.append("tokens") or 0,
    )
    return calls


def _periodic_sweep(active: set[int] | None = None) -> tuple[list[int], int]:
    """Both phases of the periodic sweep, run by this process as the live gateway."""
    my_gw = os.getpid()
    killed_or_dead, candidates = session_pid._periodic_pid_sweep(my_gw, active or set())
    killed = session_pid._kill_confirmed_and_writeback(my_gw, candidates, killed_or_dead)
    return candidates, killed


def _entries(home: Path) -> list[str]:
    path = home / session_pid._SESSION_PID_FILE
    return path.read_text(encoding="utf-8").split() if path.exists() else []


def test_a_crashed_gateways_backend_is_reaped_by_the_first_periodic_sweep_after_its_grace(
    home, tmp_path, monkeypatch, started, fake_cli
):
    beats = tmp_path / "turn.log"
    gw, cli = _spawn_gateway(home, fake_cli, beats, started)
    _crash(gw)
    assert _alive(cli), "precondition: the backend outlives the crashed gateway"
    _stub_boot_extras(monkeypatch)
    session_pid.cleanup_orphaned_sessions(narrow_with_leaders=False)  # the next boot
    assert _alive(cli), "precondition: the boot reap leaves a backend inside the spawn grace"
    candidates, killed = _periodic_sweep()
    assert cli not in candidates and _alive(cli), "inside the grace the periodic sweep waits too"
    _age_past_grace(monkeypatch, cli)
    entry = next(e for e in _entries(home) if f":{cli}:" in e)
    candidates, killed = _periodic_sweep()
    assert _wait_gone(cli), (
        f"the first periodic sweep past the grace left backend {cli} of dead gateway {gw.pid} "
        f"running: candidates {candidates}, killed {killed}, entry {entry!r}; it has written "
        f"{beats.read_text().count('tick')} ticks"
    )
    assert entry not in _entries(home), "the reaped backend's entry is removed"


def test_a_live_gateways_backend_is_left_alone_young_or_old(
    home, tmp_path, monkeypatch, started, fake_cli
):
    _gw, cli = _spawn_gateway(home, fake_cli, tmp_path / "turn.log", started)
    entries = _entries(home)
    candidates, killed = _periodic_sweep()
    assert (cli in candidates, killed, _alive(cli)) == (False, 0, True)
    _age_past_grace(monkeypatch, cli)
    candidates, killed = _periodic_sweep()
    assert (cli in candidates, killed, _alive(cli)) == (False, 0, True)
    assert _entries(home) == entries, "a live gateway's entries are not touched"


def test_an_entry_whose_pid_now_names_another_process_is_not_signalled(
    home, tmp_path, monkeypatch, started, fake_cli
):
    exited = subprocess.Popen([sys.executable, "-c", "pass"], cwd=str(tmp_path))
    exited.wait(timeout=30)
    other = subprocess.Popen(
        [str(fake_cli), "-c", _FAKE_TURN, str(tmp_path / "other.log")],
        start_new_session=True,
        cwd=str(tmp_path),
    )
    started.popens.append(other)
    started.add(other.pid)
    # A dead gateway's entry whose PID now names a process with another start token.
    (home / session_pid._SESSION_PID_FILE).write_text(f"{exited.pid}:{other.pid}:1\n")
    _age_past_grace(monkeypatch, other.pid)
    candidates, killed = _periodic_sweep()
    assert other.pid not in candidates and killed == 0
    assert _alive(other.pid), "a recycled PID is never signalled"


def test_the_boot_reap_still_kills_a_crashed_gateways_backend_past_its_grace(
    home, tmp_path, monkeypatch, started, fake_cli
):
    gw, cli = _spawn_gateway(home, fake_cli, tmp_path / "turn.log", started)
    _crash(gw)
    _age_past_grace(monkeypatch, cli)
    calls = _stub_boot_extras(monkeypatch)
    session_pid.cleanup_orphaned_sessions(narrow_with_leaders=False)
    assert _wait_gone(cli), "the boot reap kills a dead gateway's backend once past the grace"
    assert calls == ["mcp", "pid-files", "tokens"]
