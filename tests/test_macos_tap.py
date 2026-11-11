"""macOS 内录辅助程序 si-audio-tap 的进程生命周期：启动它的进程没了，它也要很快自己退出（S40）。

这些测试会真的启动 bin/si-audio-tap（需要时先编译），所以只在 macOS 上跑。没有 swiftc，
或者这台机器上内录起不来（CI 的虚拟机可能没有录音权限或音频设备）时整组跳过。只内录、不播放任何声音。

si-audio-tap 会重新启动自己（见 S1 的权限归属），一共两层进程：启动者 → 外层 → 内层（真正内录）。
"""

import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="只有 macOS 用 si-audio-tap 内录")

START_TIMEOUT_S = 10.0
EXIT_TIMEOUT_S = 3.0  # 要求 1 秒内退出（本机实测约 0.1 秒），给 CI 的慢机器留余量


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # 进程号已经被别的用户的进程重用
        return False
    return True


def children(pid: int) -> list[int]:
    out = subprocess.run(["pgrep", "-P", str(pid)], capture_output=True, text=True).stdout
    return [int(p) for p in out.split()]


def wait_gone(pids: list[int], timeout: float) -> float:
    """等这些进程全部消失，返回用了多少秒；超时返回 inf。"""
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        if not any(alive(p) for p in pids):
            return time.monotonic() - start
        time.sleep(0.01)
    return float("inf")


def wait_started(log: Path, outer: int) -> bool:
    start = time.monotonic()
    while time.monotonic() - start < START_TIMEOUT_S:
        if '"started"' in log.read_text(errors="replace"):
            return True
        if not alive(outer):
            return False
        time.sleep(0.05)
    return False


def drain(stream) -> None:
    """后台把 stdout 读空。测试时电脑可能正在放声音，管道写满了内录线程会卡住。"""

    def run():
        while stream.read(65536):
            pass

    threading.Thread(target=run, daemon=True).start()


def kill_all(pids: list[int]) -> None:
    for pid in pids:
        if alive(pid):
            os.kill(pid, signal.SIGKILL)


@pytest.fixture(scope="module")
def helper(tmp_path_factory) -> Path:
    from simul_interp.audio.macos_tap import HELPER, ensure_helper

    try:
        binary = ensure_helper()
    except RuntimeError as exc:
        if "swiftc" in str(exc) and not HELPER.exists():
            pytest.skip(str(exc))
        raise

    # 先试着启动一次：这台机器上内录起不来就整组跳过
    log = tmp_path_factory.mktemp("probe") / "stderr.log"
    with log.open("wb") as err:
        proc = subprocess.Popen([str(binary)], stdout=subprocess.PIPE, stderr=err)
    drain(proc.stdout)
    started = wait_started(log, proc.pid)
    inner = children(proc.pid)
    proc.terminate()
    try:
        proc.wait(timeout=5)
    finally:
        kill_all([proc.pid, *inner])
    if not started:
        pytest.skip(f"这台机器上内录起不来（可能没有录音权限或音频设备）：{log.read_text(errors='replace')}")
    return binary


def test_terminate_still_exits_cleanly(helper, tmp_path):
    """正常结束（Python 端 stop() 时发 SIGTERM）：外层把信号转给内层，内层清理后退出，退出码 0。"""
    log = tmp_path / "stderr.log"
    with log.open("wb") as err:
        outer = subprocess.Popen([str(helper)], stdout=subprocess.PIPE, stderr=err)
    drain(outer.stdout)
    pids = [outer.pid]
    try:
        assert wait_started(log, outer.pid), log.read_text()
        pids += children(outer.pid)
        outer.terminate()
        assert outer.wait(timeout=EXIT_TIMEOUT_S) == 0
        assert wait_gone(pids, EXIT_TIMEOUT_S) < EXIT_TIMEOUT_S
        assert '"reason":"signal"' in log.read_text()
    finally:
        kill_all(pids)


def test_exits_when_launcher_is_killed(helper, tmp_path):
    """启动它的进程被强制结束（崩溃、SIGKILL）时，两层进程都要很快退出，内层先清理。

    stdout 管道的读取端留在测试进程里、一直开着：模拟“没有声音、几乎不写数据”的情形，
    辅助程序不能靠写管道失败来发现启动者没了，只能靠盯着父进程。
    """
    log = tmp_path / "stderr.log"
    read_end, write_end = os.pipe()
    launcher_code = (
        "import subprocess, sys, time\n"
        "p = subprocess.Popen([sys.argv[1]], stdout=int(sys.argv[2]), stderr=open(sys.argv[3], 'wb'))\n"
        "print(p.pid, flush=True)\n"
        "time.sleep(600)\n"
    )
    launcher = subprocess.Popen(
        [sys.executable, "-c", launcher_code, str(helper), str(write_end), str(log)],
        stdout=subprocess.PIPE,
        text=True,
        pass_fds=(write_end,),
    )
    os.close(write_end)
    reader = os.fdopen(read_end, "rb")
    drain(reader)
    pids = []
    try:
        outer = int(launcher.stdout.readline())
        pids.append(outer)
        assert wait_started(log, outer), log.read_text()
        pids += children(outer)
        assert len(pids) == 2, f"应该有外层和内层两个进程：{pids}"

        launcher.kill()
        launcher.wait()
        elapsed = wait_gone(pids, EXIT_TIMEOUT_S)
        assert elapsed < EXIT_TIMEOUT_S, f"启动者没了 {EXIT_TIMEOUT_S} 秒后还在运行：{[p for p in pids if alive(p)]}"
        assert '"event":"stopped"' in log.read_text(), "内层应该先清理 tap 和聚合设备再退出"
    finally:
        kill_all([launcher.pid, *pids])


def test_inner_exits_when_outer_is_killed(helper, tmp_path):
    """外层被强制结束时，真正内录的内层自己发现并退出（不会被 launchd 收养后一直录下去）。"""
    log = tmp_path / "stderr.log"
    with log.open("wb") as err:
        outer = subprocess.Popen([str(helper)], stdout=subprocess.PIPE, stderr=err)
    drain(outer.stdout)  # 测试进程一直拿着读取端，内层写管道不会失败
    pids = [outer.pid]
    try:
        assert wait_started(log, outer.pid), log.read_text()
        inner = children(outer.pid)
        assert len(inner) == 1, f"应该有一个内层进程：{inner}"
        pids += inner

        outer.kill()
        outer.wait()
        elapsed = wait_gone(inner, EXIT_TIMEOUT_S)
        assert elapsed < EXIT_TIMEOUT_S, f"外层没了 {EXIT_TIMEOUT_S} 秒后内层还在运行：{inner}"
        assert '"reason":"parent_exited"' in log.read_text()
    finally:
        kill_all(pids)
