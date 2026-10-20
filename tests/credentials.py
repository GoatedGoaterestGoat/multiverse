"""Which environment variables may hold a credential, or reach one (an SSH agent's socket, an askpass helper). No test
needs one, and a failing assertion that prints an environment would put it in logs and transcripts. tests/run.py
drops them before it imports or starts anything; isolate() drops them again for each test, for a module run directly
with unittest."""
WORDS = ("TOKEN", "SECRET", "PASSWORD", "PASSWD", "_KEY", "_AUTH", "AUTHORIZATION", "CREDENTIAL", "ASKPASS")
PREFIXES = ("GH_", "GITHUB_", "AWS_", "ANTHROPIC_", "OPENAI_", "VSCODE_GIT_")  # VS Code's git credential helper
NOT_CREDENTIALS = ("GIT_AUTHOR_",)  # git's identity: GIT_AUTHOR_NAME and GIT_AUTHOR_EMAIL, which tests need


def credential(name):
    name = name.upper()
    if name.startswith(NOT_CREDENTIALS):
        return False
    return name.startswith(PREFIXES) or name.endswith("_PWD") or any(word in name for word in WORDS)
