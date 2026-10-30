"""What a test unit started outside its own process group, recorded so that tests/run.py can end it after the unit
(not a test module). isolation.track writes a record a line, JSON, to the file MV2_TEST_TRACKED names; the worker
adds what it sees in the tracked process groups (sample); Run.end_tracked reads them all.
"""
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading

SRC = Path(__file__).resolve().parents[1] / "src"
# The worker samples from a thread beside the tests, in their process, so it reaches the system only through these,
# taken at import: never through a name a test patches process-wide, such as subprocess.Popen, Path.open, os.listdir
# or time.sleep.
_Popen, _PIPE, _DEVNULL = subprocess.Popen, subprocess.PIPE, subprocess.DEVNULL
_open, _listdir, _Timer = io.open, os.listdir, threading.Timer
_PS = shutil.which("ps") or "/bin/ps"  # and ps by its path, in an environment of its own, whatever a test does to it


def _read(path):
    with _open(path) as handle:
        return handle.read()


def process_module():
    """mv2's multiverse.runtime.process, from this checkout."""
    if str(SRC) not in sys.path:
        sys.path.insert(0, str(SRC))
    from multiverse.runtime import process
    return process


def records(path):
    """The records in `path`, one JSON object a line; a line cut short when its unit was killed is skipped."""
    try:
        lines = _read(path).splitlines()
    except (OSError, TypeError):
        return []
    found = []
    for line in lines:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            found.append(record)
    return found


def processes():
    """(pid, process group, state, start) of every process, the start in the form process_started gives (on Linux
    linux:<boot id>:<start ticks>, elsewhere ps's lstart), so that it can be compared with what track() recorded;
    [] when the system cannot say."""
    if sys.platform.startswith("linux"):
        return _linux_processes()
    start = process_module().START
    try:
        ps = _Popen([_PS, "-A", "-o", "pid=,pgid=,stat=,lstart="], stdin=_DEVNULL, stdout=_PIPE, stderr=_DEVNULL,
                    text=True, env={"TZ": "UTC", "LC_ALL": "C"})
    except OSError:
        return []
    # Bounded by a timer, not communicate(timeout=): waiting with a timeout polls through time.sleep.
    stop = _Timer(10, ps.kill)
    stop.start()
    try:
        output = ps.communicate()[0]
    finally:
        stop.cancel()
    if ps.returncode:
        return []
    found = []
    for line in output.splitlines():
        fields = line.split()
        if len(fields) == 8 and fields[0].isdigit() and fields[1].isdigit() and start.fullmatch(" ".join(fields[3:])):
            found.append((int(fields[0]), int(fields[1]), fields[2], " ".join(fields[3:])))
    return found


def _linux_processes():
    """processes() from /proc: each process's state, group and start ticks from one read of its stat, with the boot
    id process_started puts in front of them, so a process that took a recorded PID after a reboot never matches."""
    try:
        boot = _read("/proc/sys/kernel/random/boot_id").strip()
        names = _listdir("/proc")
    except OSError:
        return []
    found = []
    for name in names:
        if not name.isdigit():
            continue
        try:
            text = _read(f"/proc/{name}/stat")
        except OSError:
            continue  # it has exited since the listing
        fields = text[text.rfind(")") + 2:].split() if ")" in text else []
        if len(fields) > 19 and fields[2].isdigit() and fields[19].isdigit():
            found.append((int(name), int(fields[2]), fields[0], f"linux:{boot}:{fields[19]}"))
    return found


def identities(found):
    """(pid, start) of every process the records name: tracked ones, and members seen in tracked groups."""
    return {(record["pid"], record["started"]) for record in found
            if type(record.get("pid")) is int and isinstance(record.get("started"), str)}


def sample(path):
    """Record, with its start time, every process now in a tracked process group, so that the group can be ended
    after its leader has exited (Run.end_tracked). Only while the group is provably the one tracked: a process already
    recorded in it, its leader or a member seen before, is in it now with its recorded start time. A group's number
    cannot be reused while any process is in it, so a later group with the same number never qualifies.

    isolation.track samples when it tracks a group, the worker every OBSERVE_SECONDS and once more as the unit ends."""
    if not path:
        return
    found = records(path)
    groups = {record["group"] for record in found if type(record.get("group")) is int}
    if not groups:
        return
    known = identities(found)
    present = [(pid, group, state, started) for pid, group, state, started in processes() if group in groups]
    attested = {group for pid, group, _, started in present if (pid, started) in known}
    lines = [json.dumps({"member_of": group, "pid": pid, "started": started}) + "\n"
             for pid, group, state, started in present
             if group in attested and (pid, started) not in known and not state.startswith("Z")]
    if lines:
        with _open(path, "a") as out:
            out.write("".join(lines))
