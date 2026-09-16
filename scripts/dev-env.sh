# Source from any checkout: . scripts/dev-env.sh
_mv2_common=$(git rev-parse --path-format=absolute --git-common-dir) || return
_mv2_project=$(dirname "$_mv2_common")
export MV2_CLI="$_mv2_project/bin/mv2"
export MV2_PYTHON="$_mv2_project/.venv/bin/python"
export MV2_TMUX_SOCKET="$_mv2_project/.run/tmux.sock"
export MV2_UNIVERSE="$_mv2_project/.multiverse"
export PATH="$_mv2_project/bin:$_mv2_project/.venv/bin:$PATH"
unset _mv2_common _mv2_project
