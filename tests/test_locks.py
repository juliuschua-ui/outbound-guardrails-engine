"""locks.hold(): real cross-process mutual exclusion (PLAN.md §12.2, §10 row (c)).

Every contention test spawns an actual second interpreter. A lock that only works
in-process is not a lock — it would let two `dmctl stage` runs both pass the daily-cap
check, which is the exact failure this module exists to prevent.
"""

import os
import subprocess
import sys
import time

import pytest

from dm_engine import locks

from conftest import REPO_ROOT

CHILD = """
import dataclasses, sys, time
from pathlib import Path
from dm_engine import config, locks

cfg = dataclasses.replace(config.load_config(), locks_dir=Path(sys.argv[1]))
name, action = sys.argv[2], sys.argv[3]

if action == "try":
    try:
        with locks.hold(cfg, name, timeout_s=float(sys.argv[4])):
            print("ACQUIRED", flush=True)
    except TimeoutError as exc:
        print("TIMEOUT", flush=True)
        sys.exit(9)
elif action == "hold":
    with locks.hold(cfg, name, timeout_s=30.0):
        print("HOLDING", flush=True)
        time.sleep(float(sys.argv[4]))
    print("RELEASED", flush=True)
elif action == "increment":
    counter = Path(sys.argv[4])
    with locks.hold(cfg, name, timeout_s=float(sys.argv[5])):
        value = int(counter.read_text() or "0")
        time.sleep(0.02)            # widen the race window: read, pause, write
        counter.write_text(str(value + 1))
"""


def spawn(cfg, tmp_path, *args):
    env = {
        **os.environ,
        "PYTHONPATH": str(REPO_ROOT / "src"),
        "DM_ENGINE_TEST": "1",
        "DM_ENGINE_DB": str(tmp_path / "dm.db"),
    }
    return subprocess.Popen(
        [sys.executable, "-c", CHILD, str(cfg.locks_dir), *[str(a) for a in args]],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )


def test_hold_creates_the_lock_file_and_records_the_pid(engine_cfg):
    with locks.hold(engine_cfg, "stage") as path:
        assert path == engine_cfg.locks_dir / "stage.lock"
        assert path.read_text().strip() == str(os.getpid())


def test_a_second_process_cannot_take_a_held_lock(engine_cfg, tmp_path):
    with locks.hold(engine_cfg, "stage"):
        child = spawn(engine_cfg, tmp_path, "stage", "try", 0.4)
        out, err = child.communicate(timeout=30)
    assert child.returncode == 9, f"child should have timed out. stderr: {err}"
    assert "TIMEOUT" in out and "ACQUIRED" not in out


def test_a_waiting_process_acquires_the_lock_once_released(engine_cfg, tmp_path):
    child = None
    with locks.hold(engine_cfg, "stage"):
        child = spawn(engine_cfg, tmp_path, "stage", "try", 15)
        time.sleep(0.4)
        assert child.poll() is None, "child must still be waiting while we hold the lock"
    out, err = child.communicate(timeout=30)
    assert child.returncode == 0, f"stderr: {err}"
    assert "ACQUIRED" in out


def test_concurrent_processes_serialize_on_the_lock(engine_cfg, tmp_path):
    """The cap-check race (§10 (c)) in miniature: read-pause-write from 8 processes."""
    counter = tmp_path / "counter.txt"
    counter.write_text("0")
    children = [spawn(engine_cfg, tmp_path, "cap", "increment", counter, 60)
                for _ in range(8)]
    for child in children:
        out, err = child.communicate(timeout=90)
        assert child.returncode == 0, f"child failed: {err}"
    assert int(counter.read_text()) == 8, "a lost update means the lock did not hold"


def test_unlocked_increments_actually_race(tmp_path):
    """Control: without the lock the same workload loses updates, so the test has teeth."""
    counter = tmp_path / "counter.txt"
    counter.write_text("0")
    script = (
        "import sys, time\n"
        "from pathlib import Path\n"
        "c = Path(sys.argv[1])\n"
        "v = int(c.read_text() or '0')\n"
        "time.sleep(0.2)\n"
        "c.write_text(str(v + 1))\n"
    )
    children = [subprocess.Popen([sys.executable, "-c", script, str(counter)])
                for _ in range(8)]
    for child in children:
        child.wait(timeout=60)
    assert int(counter.read_text()) < 8


def test_different_names_do_not_block_each_other(engine_cfg, tmp_path):
    with locks.hold(engine_cfg, "stage"):
        child = spawn(engine_cfg, tmp_path, "reconcile", "try", 5)
        out, err = child.communicate(timeout=30)
    assert child.returncode == 0, f"stderr: {err}"
    assert "ACQUIRED" in out


def test_lock_is_released_when_the_block_raises(engine_cfg, tmp_path):
    with pytest.raises(RuntimeError):
        with locks.hold(engine_cfg, "stage"):
            raise RuntimeError("stage blew up mid-batch")
    child = spawn(engine_cfg, tmp_path, "stage", "try", 5)
    out, err = child.communicate(timeout=30)
    assert child.returncode == 0, f"stderr: {err}"
    assert "ACQUIRED" in out


def test_timeout_error_names_the_lock(engine_cfg, tmp_path):
    child = spawn(engine_cfg, tmp_path, "stage", "hold", 5)
    assert child.stdout.readline().strip() == "HOLDING"
    try:
        with pytest.raises(TimeoutError, match="stage"):
            with locks.hold(engine_cfg, "stage", timeout_s=0.3):
                pass
    finally:
        child.kill()
        child.communicate(timeout=30)


def test_nested_hold_of_the_same_name_times_out(engine_cfg):
    """flock belongs to the open file description — hold() is NOT reentrant. Do not nest."""
    with locks.hold(engine_cfg, "stage"):
        with pytest.raises(TimeoutError):
            with locks.hold(engine_cfg, "stage", timeout_s=0.2):
                pass


@pytest.mark.parametrize("name", ["../escape", "stage/../../etc/passwd", "with space", ""])
def test_lock_names_are_restricted_to_safe_characters(engine_cfg, name):
    with pytest.raises(ValueError):
        locks.lock_path(engine_cfg, name)
