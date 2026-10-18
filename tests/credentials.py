"""Which environment variables may hold a credential. No test needs one, and a failing assertion that prints an
environment would put it in logs and transcripts. tests/run.py drops them before it imports or starts anything;
isolate() drops them again for each test, for a module run directly with unittest."""

WORDS = ("TOKEN", "SECRET", "PASSWORD", "PASSWD", "API_KEY", "ACCESS_KEY", "PRIVATE_KEY", "CREDENTIAL")
PREFIXES = ("GH_", "GITHUB_", "AWS_", "ANTHROPIC_", "OPENAI_")


def credential(name):
    name = name.upper()
    return name.startswith(PREFIXES) or any(word in name for word in WORDS)
