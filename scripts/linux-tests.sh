#!/usr/bin/env bash
# Run mv2's tests on Linux, in a container: Ubuntu 24.04 with tmux, git and a system python3, Python 3.11 and 3.13
# from uv, as a user who is not root. The tests run on a clean copy of a checkout, made from git's own file lists, so
# nothing ignored (.venv, the checkout's .multiverse) and nothing of the host's environment goes in, except the
# record of past test durations that tests/run.py plans its shards by.
#
#   scripts/linux-tests.sh [--checkout DIR] [--commit REV] [--python 3.11|3.13] [-j N] [MODULE...]
#
# With no MODULE it runs the whole suite. By default it copies the working tree of the checkout this script is in,
# uncommitted changes included, and each initialized submodule's working tree. --commit copies that commit instead,
# with the submodule commits it pins. The exit code is tests/run.py's. Needs Docker; the first run builds the image.
set -euo pipefail

usage() { sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

checkout=$(cd "$(dirname "$0")/.." && pwd)
commit=""
python="3.13"
runner_args=()
while [ $# -gt 0 ]; do
    case "$1" in
        --checkout) checkout=$(cd "$2" && git rev-parse --show-toplevel); shift 2 ;;
        --commit) commit=$2; shift 2 ;;
        --python) python=$2; shift 2 ;;
        -j) runner_args+=(-j "$2"); shift 2 ;;
        -h|--help) usage ;;
        -*) echo "linux-tests: unknown option $1" >&2; usage 2 ;;
        *) runner_args+=("$1"); shift ;;
    esac
done
case "$python" in 3.11|3.13) ;; *) echo "linux-tests: --python is 3.11 or 3.13" >&2; exit 2 ;; esac
command -v docker >/dev/null || { echo "linux-tests: needs Docker" >&2; exit 2; }

# The image, rebuilt only when this text changes: its tag is the text's hash.
dockerfile='FROM ubuntu:24.04
RUN apt-get update && apt-get install -y --no-install-recommends tmux git ca-certificates curl python3 procps \
    && rm -rf /var/lib/apt/lists/*
RUN useradd --create-home tester
USER tester
WORKDIR /home/tester
RUN curl -LsSf https://astral.sh/uv/0.7.20/install.sh | env UV_NO_MODIFY_PATH=1 sh
ENV PATH=/home/tester/.local/bin:$PATH
RUN uv python install 3.11 3.13 && mkdir -p /home/tester/.cache/uv'
digest='import hashlib, sys; print(hashlib.sha256(sys.stdin.buffer.read()).hexdigest())'
hash=$(printf '%s' "$dockerfile" | python3 -c "$digest")
image="mv2-linux-tests:${hash:0:12}"
if ! docker image inspect "$image" >/dev/null 2>&1; then
    echo "linux-tests: building $image (once)" >&2
    printf '%s\n' "$dockerfile" | docker build --quiet --tag "$image" - >/dev/null
fi

copy=$(mktemp -d "${TMPDIR:-/tmp}/mv2-linux-tests-XXXXXX")
trap 'rm -rf "$copy"' EXIT
python3 - "$checkout" "$commit" "$copy" <<'PY'
"""Copy a checkout's files, or a commit's, into a new directory, submodules included; never anything git ignores."""
import os
import shutil
import subprocess
import sys

checkout, commit, copy = sys.argv[1:]


def git(directory, *args):
    return subprocess.run(["git", "-C", directory, *args], capture_output=True, check=True).stdout


def gitlinks(directory, rev=None):
    """(path, commit) of each submodule: in the index, or in commit `rev`."""
    listing = git(directory, "ls-tree", "-r", "-z", rev) if rev else git(directory, "ls-files", "-s", "-z")
    found = []
    for entry in listing.split(b"\0"):
        if entry.startswith(b"160000 "):
            meta, path = entry.split(b"\t", 1)
            found.append((os.fsdecode(path), meta.split()[-1 if rev else 1].decode()))
    return found


def working_tree(source, target):
    links = {path for path, _ in gitlinks(source)}
    files = git(source, "ls-files", "-z", "--cached", "--others", "--exclude-standard").split(b"\0")
    for name in filter(None, (os.fsdecode(name) for name in files)):
        if name.endswith("/"):  # an untracked repository or worktree inside this one: not part of the checkout
            print(f"linux-tests: skipping {name}, a repository of its own", file=sys.stderr)
            continue
        if name in links or name.split("/")[0] == ".multiverse" or not os.path.lexists(os.path.join(source, name)):
            continue  # a submodule (below), the universe, or a file deleted but not yet committed
        os.makedirs(os.path.dirname(os.path.join(target, name)) or target, exist_ok=True)
        shutil.copy2(os.path.join(source, name), os.path.join(target, name), follow_symlinks=False)
    for path in sorted(links):
        if os.path.exists(os.path.join(source, path, ".git")):
            working_tree(os.path.join(source, path), os.path.join(target, path))
        else:
            print(f"linux-tests: submodule {path} is not initialized: its tests will not run", file=sys.stderr)


def at_commit(source, rev, target):
    os.makedirs(target, exist_ok=True)
    archive = subprocess.run(["git", "-C", source, "archive", "--format=tar", rev], capture_output=True, check=True)
    subprocess.run(["tar", "-x", "-C", target], input=archive.stdout, check=True)
    for path, sub in gitlinks(source, rev):
        try:
            at_commit(os.path.join(source, path), sub, os.path.join(target, path))
        except subprocess.CalledProcessError:
            print(f"linux-tests: submodule {path} at {sub[:7]} is not here: its tests will not run", file=sys.stderr)


if commit:
    at_commit(checkout, commit, copy)
else:
    working_tree(checkout, copy)
durations = os.path.join(checkout, ".run", "test-durations.json")  # only to plan the run's shards
if os.path.exists(durations):
    os.makedirs(os.path.join(copy, ".run"), exist_ok=True)
    shutil.copy2(durations, os.path.join(copy, ".run", "test-durations.json"))
PY

echo "linux-tests: $image, Python $python, ${commit:+commit $commit of }$checkout" >&2
inner='set -euo pipefail
python=$1; shift
mkdir -p ~/work && tar -x -C ~/work && cd ~/work
uv sync --frozen --quiet --python "$python"
for name in $(compgen -e | grep "^MV2_" || true); do unset "$name"; done
exec .venv/bin/python tests/run.py "$@"'
set +e
tar -C "$copy" -cf - . | docker run --rm --interactive --init --volume mv2-linux-tests-uv:/home/tester/.cache/uv \
    "$image" bash -c "$inner" bash "$python" ${runner_args[@]+"${runner_args[@]}"}
status=$?
exit "$status"
