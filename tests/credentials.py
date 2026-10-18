"""Which environment variables may hold a credential, or reach one (an SSH agent's socket, an askpass helper). No test
needs one, and a failing assertion that prints an environment would put it in logs and transcripts. tests/run.py
drops them before it imports or starts anything; isolate() drops them again for each test, for a module run directly
with unittest."""
import re

WORDS = ("TOKEN", "SECRET", "PASSWORD", "PASSWD", "_KEY", "CREDENTIAL", "ASKPASS")
PREFIXES = ("GH_", "GITHUB_", "AWS_", "ANTHROPIC_", "OPENAI_")
AUTH = re.compile(r"_AUTH(?!OR)")  # NPM_AUTH, SSH_AUTH_SOCK, but not git's GIT_AUTHOR_NAME


def credential(name):
    name = name.upper()
    return (name.startswith(PREFIXES) or name.endswith("_PWD") or any(word in name for word in WORDS)
            or bool(AUTH.search(name)))
