"""A plain ssh tunnel's ProxyCommand ends with ssh, on every exit (POSIX).

The tunnel runs ssh under the group keeper (``_GROUP_KEEPER``), which leads the group
and ends it once, by itself, while it is alive. One test per row of the every-exit
table in the change's description: each drives a real ``_SshTunnel`` through the real
keeper around a stand-in ``ssh`` that starts a ProxyCommand-like child in its group,
and checks that neither outlives the exit. Only the pids the stand-in recorded are
ever signalled by the tests.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from kiro_crew import platform_compat
from kiro_crew.instances import ssh_tunnel_manager as stm
from kiro_crew.instances.ssh_tunnel_manager import TunnelState, _SshTunnel

pytestmark = pytest.mark.skipif(not platform_compat.IS_POSIX, reason="the keeper is POSIX only")

# A stand-in ``ssh``: it starts a long-lived child the way ssh starts its ProxyCommand
# (same group, pipes inherited), records both pids, then waits. ``ignore-term``: ssh
# ignores SIGTERM, so only the keeper's SIGKILL ends it. ``count-term``: the child
# appends a line to ``<pidfile>.terms`` per SIGTERM and lives 3 s. ``exit``: ssh exits
# 255 at once.
_SSH = r"""
import json, os, signal, subprocess, sys, time
out, mode = sys.argv[1], sys.argv[2]
if mode == "ignore-term":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
code = "import time; time.sleep(120)"
if mode == "count-term":
    code = (
        "import signal, sys, time\n"
        "signal.signal(signal.SIGTERM, lambda *_: open(sys.argv[1], 'a').write('TERM\\n'))\n"
        "open(sys.argv[1] + '.ready', 'w').close()\n"
        "end = time.monotonic() + 3\n"
        "while time.monotonic() < end:\n"
        "    time.sleep(0.02)\n"
    )
proxy = subprocess.Popen(
    [sys.executable, "-c", code, out + ".terms"], cwd=os.path.dirname(out)
)
with open(out + ".tmp", "w") as f:
    json.dump({"ssh": os.getpid(), "proxy": proxy.pid}, f)
os.replace(out + ".tmp", out)
if mode == "exit":
    sys.exit(255)
time.sleep(120)
"""


def _alive(pid: int) -> bool:
    """Whether *pid* is a live process: a zombie, or one being released, is not."""
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as f:
            return f.read().rsplit(")", 1)[1].split()[0] not in ("Z", "X", "x")
    except (FileNotFoundError, ProcessLookupError):
        return not os.path.isdir("/proc/self")
    except OSError:
        return True


async def _gone(*pids: int, within: float = 5.0) -> bool:
    deadline = time.monotonic() + within
    while time.monotonic() < deadline and any(_alive(p) for p in pids):
        await asyncio.sleep(0.02)
    return not any(_alive(p) for p in pids)


async def _pids(pidfile: Path, within: float = 10.0) -> dict[str, int]:
    deadline = time.monotonic() + within
    while time.monotonic() < deadline and not pidfile.exists():
        await asyncio.sleep(0.02)
    assert pidfile.exists(), "the stand-in ssh never started"
    return json.loads(pidfile.read_text(encoding="utf-8"))


def _pipes() -> int:
    """How many pipe fds this process holds (Linux; 0 where /proc is absent)."""
    root = "/proc/self/fd"
    if not os.path.isdir(root):
        return 0
    count = 0
    for name in os.listdir(root):
        with contextlib.suppress(OSError):
            count += os.readlink(f"{root}/{name}").startswith("pipe:")
    return count


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return int(port)


@pytest.fixture
def listening_port() -> Iterator[int]:
    """A port the test holds open, so the readiness connect is answered."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(16)
    yield int(s.getsockname()[1])
    s.close()


@pytest.fixture
def standin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Callable[..., tuple[_SshTunnel, Path]]]:
    """Build tunnels whose ssh is the stand-in; at teardown end what they left."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    made: list[tuple[_SshTunnel, Path]] = []

    def make(*, port: int, mode: str = "serve", timeout: float = 10) -> tuple[_SshTunnel, Path]:
        pidfile = tmp_path / f"ssh{len(made)}.json"
        tunnel = _SshTunnel("cd-1", "cd-1-alias", port, 7777, connect_timeout_secs=timeout)
        argv = [sys.executable, "-c", _SSH, str(pidfile), mode]
        monkeypatch.setattr(tunnel, "_build_argv", lambda: argv)
        made.append((tunnel, pidfile))
        return tunnel, pidfile

    yield make
    for tunnel, pidfile in made:
        tunnel._end_group()
        if pidfile.exists():
            for pid in json.loads(pidfile.read_text(encoding="utf-8")).values():
                with contextlib.suppress(ProcessLookupError):
                    os.kill(pid, signal.SIGKILL)  # a recorded pid only


class _KillsSshOnCancel:
    """Stands in for the monitor task: kills the stand-in ssh as stop() cancels it."""

    def __init__(self, task: asyncio.Task[None], ssh_pid: int) -> None:
        self._task = task
        self._ssh = ssh_pid

    def done(self) -> bool:
        return self._task.done()

    def cancel(self, *args: Any, **kwargs: Any) -> bool:
        os.kill(self._ssh, signal.SIGKILL)  # the recorded pid only
        return bool(self._task.cancel(*args, **kwargs))

    def __await__(self) -> Any:
        return self._task.__await__()


def _run_keeper(
    tmp_path: Path, mode: str, grace: str = "5", source: str | None = None
) -> tuple[subprocess.Popen[bytes], int]:
    """Start the keeper around the stand-in ssh directly: (keeper, control write end).

    *source* replaces the keeper's source, to run it under a prelude.
    """
    r, w = os.pipe()
    keeper = subprocess.Popen(
        [sys.executable, "-I", "-S", "-c", source or stm._GROUP_KEEPER, str(r), grace]
        + [sys.executable, "-c", _SSH, str(tmp_path / "ssh.json"), mode],
        pass_fds=(r,),
        start_new_session=True,
        cwd=tmp_path,
    )
    os.close(r)
    os.write(w, b"g")
    return keeper, w


def _pids_sync(pidfile: Path) -> dict[str, int]:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not pidfile.exists():
        time.sleep(0.02)
    return json.loads(pidfile.read_text(encoding="utf-8"))


def _gone_sync(*pids: int, within: float = 5.0) -> bool:
    deadline = time.monotonic() + within
    while time.monotonic() < deadline and any(_alive(p) for p in pids):
        time.sleep(0.02)
    return not any(_alive(p) for p in pids)


# start()


@pytest.mark.asyncio
async def test_row1_an_argv_build_that_fails_spawns_nothing(standin: Any, monkeypatch: Any) -> None:
    tunnel, _ = standin(port=_free_port())

    def _fail() -> list[str]:
        raise RuntimeError("no ssh on PATH")

    monkeypatch.setattr(tunnel, "_build_argv", _fail)
    pipes = _pipes()
    with pytest.raises(RuntimeError):
        await tunnel.start()
    assert tunnel._proc is None and tunnel._keeper_ctrl is None and _pipes() == pipes


@pytest.mark.asyncio
async def test_row2_a_spawn_that_fails_closes_both_ends_of_the_control_pipe(
    standin: Any, monkeypatch: Any
) -> None:
    tunnel, _ = standin(port=_free_port())

    async def _refuse(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError("exec failed")

    monkeypatch.setattr(stm.asyncio, "create_subprocess_exec", _refuse)
    pipes = _pipes()
    assert await tunnel.start() is False
    assert tunnel.status.state == TunnelState.ERROR
    assert tunnel._keeper_ctrl is None and _pipes() == pipes


@pytest.mark.asyncio
async def test_row3_a_spawn_cancelled_after_the_fork_starts_no_ssh(
    standin: Any, monkeypatch: Any
) -> None:
    tunnel, pidfile = standin(port=_free_port())
    real_exec = asyncio.create_subprocess_exec
    spawned: list[Any] = []

    async def _then_cancelled(*args: Any, **kwargs: Any) -> Any:
        spawned.append(await real_exec(*args, **kwargs))
        raise asyncio.CancelledError

    monkeypatch.setattr(stm.asyncio, "create_subprocess_exec", _then_cancelled)
    with pytest.raises(asyncio.CancelledError):
        await tunnel.start()
    # The keeper read end of file before its go byte, so it started nothing.
    assert await asyncio.wait_for(spawned[0].wait(), 10) == 0
    assert not pidfile.exists() and tunnel._keeper_ctrl is None


@pytest.mark.asyncio
async def test_row8_a_start_cancelled_in_its_readiness_wait_ends_ssh_and_the_proxycommand(
    standin: Any,
) -> None:
    tunnel, pidfile = standin(port=_free_port(), timeout=30)
    task = asyncio.create_task(tunnel.start())
    pids = await _pids(pidfile)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await _gone(pids["ssh"], pids["proxy"]), "a cancelled start left its tunnel running"


@pytest.mark.asyncio
async def test_row9_a_cancelled_start_whose_ssh_then_dies_leaves_nothing_for_stop(
    standin: Any,
) -> None:
    # start() is cancelled in its readiness wait, then ssh dies before stop(): stop()
    # must not leave the ProxyCommand running.
    tunnel, pidfile = standin(port=_free_port(), timeout=30)
    task = asyncio.create_task(tunnel.start())
    pids = await _pids(pidfile)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    with contextlib.suppress(ProcessLookupError):
        os.kill(pids["ssh"], signal.SIGKILL)  # the recorded pid only
    await tunnel.stop()
    assert await _gone(pids["proxy"]), "stop() after a cancelled start left the ProxyCommand"


@pytest.mark.asyncio
async def test_row11_an_error_in_the_readiness_wait_ends_ssh_and_the_proxycommand(
    standin: Any, monkeypatch: Any
) -> None:
    tunnel, pidfile = standin(port=_free_port(), timeout=30)

    async def _reachable_then_failing() -> bool:
        await _pids(pidfile)
        raise RuntimeError("probe failed")

    monkeypatch.setattr(tunnel, "_port_reachable", _reachable_then_failing)
    with pytest.raises(RuntimeError):
        await tunnel.start()
    pids = await _pids(pidfile)
    assert await _gone(pids["ssh"], pids["proxy"])


@pytest.mark.asyncio
async def test_row6_ssh_exiting_in_the_readiness_wait_takes_the_proxycommand_with_it(
    standin: Any,
) -> None:
    tunnel, pidfile = standin(port=_free_port(), timeout=30)
    task = asyncio.create_task(tunnel.start())
    pids = await _pids(pidfile)
    os.kill(pids["ssh"], signal.SIGKILL)  # the recorded pid only
    assert await asyncio.wait_for(task, 15) is False
    assert tunnel.status.state == TunnelState.ERROR
    assert await _gone(pids["proxy"]), "ssh died while starting and its ProxyCommand ran on"


@pytest.mark.asyncio
async def test_row7_a_readiness_timeout_ends_ssh_and_the_proxycommand(standin: Any) -> None:
    tunnel, pidfile = standin(port=_free_port(), timeout=2)
    assert await tunnel.start() is False
    pids = await _pids(pidfile)
    assert await _gone(pids["ssh"], pids["proxy"])


@pytest.mark.asyncio
async def test_row12_a_start_cancelled_in_its_teardown_still_ends_ssh(
    standin: Any, monkeypatch: Any
) -> None:
    monkeypatch.setattr(stm, "_KEEPER_TERM_GRACE_SECS", 0.5)
    tunnel, pidfile = standin(port=_free_port(), mode="ignore-term", timeout=2)
    real_terminate = tunnel._terminate
    tearing = asyncio.Event()

    async def _terminate() -> None:
        tearing.set()
        await real_terminate()

    monkeypatch.setattr(tunnel, "_terminate", _terminate)
    task = asyncio.create_task(tunnel.start())
    await asyncio.wait_for(tearing.wait(), 15)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    pids = await _pids(pidfile)
    assert await _gone(pids["ssh"], pids["proxy"]), "the cancel stopped the SIGKILL escalation"


# The monitor


@pytest.mark.asyncio
async def test_row13_ssh_exiting_on_its_own_takes_the_proxycommand_with_it(
    standin: Any, listening_port: int
) -> None:
    # ssh exits on its own (a dropped connection), with no stop().
    tunnel, pidfile = standin(port=listening_port)
    assert await tunnel.start() is True
    pids = await _pids(pidfile)
    os.kill(pids["ssh"], signal.SIGKILL)  # the recorded pid only
    assert await _gone(pids["proxy"]), "ssh exited on its own and its ProxyCommand ran on"
    await asyncio.wait_for(tunnel._monitor_task, 10)
    assert tunnel.status.state == TunnelState.ERROR and tunnel._keeper_ctrl is None
    await tunnel.stop()


@pytest.mark.asyncio
async def test_row15_ssh_dying_as_stop_cancels_the_monitor_still_ends_the_proxycommand(
    standin: Any, listening_port: int
) -> None:
    # ssh dies in the loop turn in which stop() cancels the monitor.
    tunnel, pidfile = standin(port=listening_port)
    assert await tunnel.start() is True
    pids = await _pids(pidfile)
    tunnel._monitor_task = _KillsSshOnCancel(tunnel._monitor_task, pids["ssh"])  # type: ignore
    await tunnel.stop()
    assert await _gone(pids["proxy"])


@pytest.mark.asyncio
async def test_row20_ssh_dying_between_the_monitor_cancel_and_the_teardown(
    standin: Any, listening_port: int, monkeypatch: Any
) -> None:
    tunnel, pidfile = standin(port=listening_port)
    assert await tunnel.start() is True
    pids = await _pids(pidfile)
    real_drain = tunnel._finish_stdout_drain

    async def _drain(*args: Any, **kwargs: Any) -> None:
        os.kill(pids["ssh"], signal.SIGKILL)  # the recorded pid only
        await real_drain(*args, **kwargs)

    monkeypatch.setattr(tunnel, "_finish_stdout_drain", _drain)
    await tunnel.stop()
    assert await _gone(pids["proxy"])


@pytest.mark.asyncio
async def test_row18_an_error_after_the_monitor_saw_the_exit_leaves_the_group_ended(
    standin: Any, listening_port: int, monkeypatch: Any
) -> None:
    tunnel, pidfile = standin(port=listening_port)
    assert await tunnel.start() is True
    pids = await _pids(pidfile)

    async def _failing_capture() -> None:
        raise RuntimeError("stderr unreadable")

    monkeypatch.setattr(tunnel, "_capture_stderr", _failing_capture)
    os.kill(pids["ssh"], signal.SIGKILL)  # the recorded pid only
    with pytest.raises(RuntimeError):
        await asyncio.wait_for(tunnel._monitor_task, 10)
    assert await _gone(pids["proxy"]) and tunnel._keeper_ctrl is None


# stop()


@pytest.mark.asyncio
async def test_row19_stop_ends_ssh_and_the_proxycommand(standin: Any, listening_port: int) -> None:
    tunnel, pidfile = standin(port=listening_port)
    assert await tunnel.start() is True
    pids = await _pids(pidfile)
    await tunnel.stop()
    assert await _gone(pids["ssh"], pids["proxy"]), "the ProxyCommand outlived stop()"


@pytest.mark.asyncio
async def test_row19b_stop_kills_an_ssh_that_ignores_sigterm_after_the_grace(
    standin: Any, listening_port: int, monkeypatch: Any
) -> None:
    monkeypatch.setattr(stm, "_KEEPER_TERM_GRACE_SECS", 0.5)
    tunnel, pidfile = standin(port=listening_port, mode="ignore-term")
    assert await tunnel.start() is True
    pids = await _pids(pidfile)
    await tunnel.stop()
    assert await _gone(pids["ssh"], pids["proxy"])


@pytest.mark.asyncio
async def test_row21_a_stop_cancelled_in_its_wait_still_ends_ssh(
    standin: Any, listening_port: int, monkeypatch: Any
) -> None:
    monkeypatch.setattr(stm, "_KEEPER_TERM_GRACE_SECS", 0.5)
    tunnel, pidfile = standin(port=listening_port, mode="ignore-term")
    assert await tunnel.start() is True
    pids = await _pids(pidfile)
    task = asyncio.create_task(tunnel.stop())
    deadline = time.monotonic() + 5
    while tunnel._keeper_ctrl is not None and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    assert tunnel._keeper_ctrl is None, "precondition: stop() is in its teardown wait"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await _gone(pids["ssh"], pids["proxy"]), "the cancel stopped the SIGKILL escalation"


@pytest.mark.asyncio
async def test_row22_stop_long_after_ssh_exited_signals_no_group_number(
    standin: Any, listening_port: int, monkeypatch: Any
) -> None:
    # Nothing in the gateway signals a group by number, so a number reused since
    # ssh exited cannot be reached.
    tunnel, pidfile = standin(port=listening_port)
    assert await tunnel.start() is True
    pids = await _pids(pidfile)

    def _refuse(*_args: Any) -> Any:
        raise AssertionError("the gateway signalled a process group")

    monkeypatch.setattr(platform_compat, "kill_process_tree", _refuse)
    monkeypatch.setattr(platform_compat, "kill_process_group", _refuse)
    monkeypatch.setattr(os, "killpg", _refuse)
    os.kill(pids["ssh"], signal.SIGKILL)  # the recorded pid only
    await asyncio.wait_for(tunnel._monitor_task, 10)
    await tunnel.stop()
    assert await _gone(pids["proxy"])


# The keeper itself


def test_the_keeper_ends_its_group_once(tmp_path: Path) -> None:
    keeper, w = _run_keeper(tmp_path, "count-term")
    pids = _pids_sync(tmp_path / "ssh.json")
    ready = tmp_path / "ssh.json.terms.ready"
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not ready.exists():
        time.sleep(0.02)
    assert ready.exists(), "precondition: the child counts SIGTERMs"
    # Three ends at once: a SIGTERM to the keeper, end of file, ssh's own exit.
    os.kill(keeper.pid, signal.SIGTERM)
    os.close(w)
    with contextlib.suppress(ProcessLookupError):
        os.kill(pids["ssh"], signal.SIGKILL)  # the recorded pid only
    keeper.wait(10)
    assert _gone_sync(pids["proxy"], within=6)
    terms = (tmp_path / "ssh.json.terms").read_text(encoding="utf-8").splitlines()
    assert terms == ["TERM"], terms


def test_the_keeper_exits_with_ssh_status(tmp_path: Path) -> None:
    keeper, w = _run_keeper(tmp_path, "exit")
    try:
        assert keeper.wait(10) == 255
        assert _gone_sync(_pids_sync(tmp_path / "ssh.json")["proxy"])
    finally:
        os.close(w)
    (tmp_path / "ssh.json").unlink()
    keeper, w = _run_keeper(tmp_path, "serve")
    try:
        pids = _pids_sync(tmp_path / "ssh.json")
        os.kill(pids["ssh"], signal.SIGKILL)  # the recorded pid only
        assert keeper.wait(10) == -signal.SIGKILL
        assert _gone_sync(pids["proxy"])
    finally:
        os.close(w)


def test_row24_end_of_file_on_the_control_pipe_ends_the_group(tmp_path: Path) -> None:
    # What the kernel does when the gateway process ends: its write end closes.
    keeper, w = _run_keeper(tmp_path, "serve")
    pids = _pids_sync(tmp_path / "ssh.json")
    os.close(w)
    assert keeper.wait(10) == -signal.SIGTERM
    assert _gone_sync(pids["ssh"], pids["proxy"])


def test_a_keeper_that_cannot_start_a_thread_starts_no_ssh(tmp_path: Path) -> None:
    # The OS refuses the keeper a thread (a process or memory limit). Without its threads
    # nothing would end ssh's group, so the keeper must exit before it starts ssh.
    started = tmp_path / "popen.called"
    prelude = (
        "import subprocess, threading\n"
        "def _refuse(self):\n"
        "    raise RuntimeError('cannot start a new thread')\n"
        "threading.Thread.start = _refuse\n"
        "_popen = subprocess.Popen\n"
        "def _record(*a, **k):\n"
        f"    open({str(started)!r}, 'w').close()\n"
        "    return _popen(*a, **k)\n"
        "subprocess.Popen = _record\n"
    )
    keeper, w = _run_keeper(tmp_path, "serve", source=prelude + stm._GROUP_KEEPER)
    try:
        assert keeper.wait(10) == 1
    finally:
        os.close(w)
        if started.exists():  # a keeper that started ssh anyway: end what it recorded
            for pid in _pids_sync(tmp_path / "ssh.json").values():
                with contextlib.suppress(ProcessLookupError):
                    os.kill(pid, signal.SIGKILL)  # the recorded pids only
    assert not started.exists(), "the keeper started ssh with no thread to end its group"
