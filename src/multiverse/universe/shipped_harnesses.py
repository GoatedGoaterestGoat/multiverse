"""Every harness.json text mv2's bundled templates shipped before refresh saved the version a file is based
on, by fingerprint (as recorded in template-files.json).

A universe installed or refreshed by an earlier mv2 records only its harness file's fingerprint. When such a
file has been edited and the template has changed, refresh finds the text it came from here and merges
three-way from it, so a key the universe deleted stays deleted. The table is frozen: refresh saves the base
itself from then on, so later template versions never need adding."""

TEXTS = {
    # Silicon23/harnesses/claude/harness.json
    'sha256:2d6b37917b514c887c53d68ec3ddd327fd8b292bd4451e75016e564e0f236064': (
        '{\n'
        '  "schema": 2,\n'
        '  "adapter": "claude",\n'
        '  "description": "Claude Code; prompt-led coordination and an assigned native session UUID'
        '.",\n'
        '  "settings": {\n'
        '    "args": [\n'
        '      "--dangerously-skip-permissions"\n'
        '    ],\n'
        '    "model_names": {\n'
        '      "claude-opus-5-5": "Opus",\n'
        '      "opus": "Opus",\n'
        '      "claude-sonnet-5-5": "Sonnet",\n'
        '      "sonnet": "Sonnet",\n'
        '      "claude-haiku-5-5": "Haiku",\n'
        '      "haiku": "Haiku"\n'
        '    }\n'
        '  }\n'
        '}\n'
    ),
    # Silicon23/harnesses/claude/harness.json
    'sha256:77a091dec7ccf8aa50f7c3762f708432222247bbacdf5c1bafb8ba57af27c1ac': (
        '{\n'
        '  "schema": 2,\n'
        '  "adapter": "claude",\n'
        '  "description": "Claude Code; prompt-led coordination and an assigned native session UUID'
        '.",\n'
        '  "settings": {\n'
        '    "args": [\n'
        '      "--dangerously-skip-permissions"\n'
        '    ]\n'
        '  }\n'
        '}\n'
    ),
    # Silicon23/harnesses/claude/harness.json
    'sha256:e4e0c44301468c25743d5c8342195430b798e999fd4d13f0586907902f53842a': (
        '{\n'
        '  "schema": 2,\n'
        '  "adapter": "claude",\n'
        '  "description": "Claude Code; prompt-led coordination and an assigned native session UUID'
        '.",\n'
        '  "settings": {\n'
        '    "args": [\n'
        '      "--dangerously-skip-permissions"\n'
        '    ],\n'
        '    "model": "claude-opus-5-5",\n'
        '    "models": {\n'
        '      "claude-fable-5-2": {},\n'
        '      "claude-opus-5-5": {},\n'
        '      "claude-sonnet-5-5": {},\n'
        '      "claude-haiku-5-5": {}\n'
        '    },\n'
        '    "model_names": {\n'
        '      "claude-fable-5-2": "Fable",\n'
        '      "fable": "Fable",\n'
        '      "claude-opus-5-5": "Opus",\n'
        '      "opus": "Opus",\n'
        '      "claude-sonnet-5-5": "Sonnet",\n'
        '      "sonnet": "Sonnet",\n'
        '      "claude-haiku-5-5": "Haiku",\n'
        '      "haiku": "Haiku"\n'
        '    }\n'
        '  }\n'
        '}\n'
    ),
    # Silicon23/harnesses/codex/harness.json
    'sha256:2a521f6602e7ef63fab864bf5e333ca90254985b1040710084bd188f2e1cb685': (
        '{\n'
        '  "schema": 2,\n'
        '  "adapter": "codex",\n'
        '  "description": "Codex CLI; prompt-led coordination and explicit native session resume.",'
        '\n'
        '  "settings": {\n'
        '    "args": [\n'
        '      "--dangerously-bypass-approvals-and-sandbox"\n'
        '    ]\n'
        '  }\n'
        '}\n'
    ),
    # Silicon23/harnesses/codex/harness.json
    'sha256:9d2fa246554dd4f252bad35a66108a5b5d2ce7d938263eb7fa0d4b43923cbde8': (
        '{\n'
        '  "schema": 2,\n'
        '  "adapter": "codex",\n'
        '  "description": "Codex CLI; prompt-led coordination and explicit native session resume.",'
        '\n'
        '  "settings": {\n'
        '    "args": [\n'
        '      "--dangerously-bypass-approvals-and-sandbox"\n'
        '    ],\n'
        '    "model": "gpt-6.1-sol",\n'
        '    "models": {\n'
        '      "gpt-6-astra": {},\n'
        '      "gpt-6.1-sol": {},\n'
        '      "gpt-6.1-terra": {},\n'
        '      "gpt-6-luna": {}\n'
        '    },\n'
        '    "model_names": {\n'
        '      "gpt-6-astra": "Astra",\n'
        '      "gpt-6.1-sol": "Sol",\n'
        '      "gpt-6.1-terra": "Terra",\n'
        '      "gpt-6-luna": "Luna"\n'
        '    }\n'
        '  }\n'
        '}\n'
    ),
    # Silicon23/harnesses/codex/harness.json
    'sha256:a42702870fb841f005c74f7be305267f0cb7fb065e6446d9879c6c53a04056bf': (
        '{\n'
        '  "schema": 2,\n'
        '  "adapter": "codex",\n'
        '  "description": "Codex CLI; prompt-led coordination and explicit native session resume.",'
        '\n'
        '  "settings": {\n'
        '    "args": [\n'
        '      "--dangerously-bypass-approvals-and-sandbox"\n'
        '    ],\n'
        '    "model_names": {\n'
        '      "gpt-6-astra": "Astra",\n'
        '      "gpt-6.1-sol": "Sol",\n'
        '      "gpt-6.1-terra": "Terra",\n'
        '      "gpt-6-luna": "Luna"\n'
        '    }\n'
        '  }\n'
        '}\n'
    ),
    # base/harnesses/claude/harness.json
    'sha256:2580234fdc450e3b031933415415df414aa482cf78040755c458a96760133758': (
        '{\n'
        '  "schema": 1,\n'
        '  "description": "Claude Code; prompt-led coordination and an assigned native session UUID'
        '.",\n'
        '  "checked_cli_version": "2.1.231",\n'
        '  "session": "assigned",\n'
        '  "spawn": ["claude", "--session-id", "{session_id}", "--append-system-prompt", "{bootstra'
        'p}", "--add-dir", "{agent_dir}", "--", "{task}"],\n'
        '  "resume": ["claude", "--resume", "{session_id}", "--append-system-prompt", "{bootstrap}"'
        ', "--add-dir", "{agent_dir}"],\n'
        '  "model_args": ["--model", "{model}"],\n'
        '  "unset_env": ["CLAUDECODE"],\n'
        '  "docs": "https://code.claude.com/docs/en/cli-reference"\n'
        '}\n'
    ),
    # base/harnesses/claude/harness.json
    'sha256:71972e1bf054620a0b0997b7addfe99d9d63fecee01877a9018e0686de97c077': (
        '{\n'
        '  "schema": 1,\n'
        '  "description": "Claude Code; prompt-led coordination and an assigned native session UUID'
        '.",\n'
        '  "checked_cli_version": "2.1.231",\n'
        '  "session": "assigned",\n'
        '  "spawn": ["claude", "--session-id", "{session_id}", "--append-system-prompt", "{bootstra'
        'p}", "--add-dir", "{agent_dir}", "--", "{task}"],\n'
        '  "resume": ["claude", "--resume", "{session_id}", "--append-system-prompt", "{bootstrap}"'
        ', "--add-dir", "{agent_dir}"],\n'
        '  "model_args": ["--model", "{model}"],\n'
        '  "unset_env": ["CODEX_THREAD_ID", "CLAUDECODE"],\n'
        '  "docs": "https://code.claude.com/docs/en/cli-reference"\n'
        '}\n'
    ),
    # base/harnesses/claude/harness.json
    'sha256:bef65d56f82abd0c1907cb57bbbfb4fbe446c1b02084f7b046fe8ff15c602a2a': (
        '{\n'
        '  "schema": 2,\n'
        '  "adapter": "claude",\n'
        '  "description": "Claude Code; prompt-led coordination and an assigned native session UUID'
        '.",\n'
        '  "settings": {\n'
        '    "model": "claude-opus-5-5"\n'
        '  }\n'
        '}\n'
    ),
    # base/harnesses/claude/harness.json
    'sha256:e1b4a40925a758a8feec80b81ccb85d5f863aee224540c2f1e2d20a287e39243': (
        '{\n'
        '  "schema": 2,\n'
        '  "adapter": "claude",\n'
        '  "description": "Claude Code; prompt-led coordination and an assigned native session UUID'
        '.",\n'
        '  "settings": {}\n'
        '}\n'
    ),
    # base/harnesses/codex/harness.json
    'sha256:0e041e43a7a4e8b760f11388521fd46b51445e722ce2e7f9de3791de9a33c7da': (
        '{\n'
        '  "schema": 2,\n'
        '  "adapter": "codex",\n'
        '  "description": "Codex CLI; prompt-led coordination and explicit native session resume.",'
        '\n'
        '  "settings": {\n'
        '    "model": "gpt-6.1-sol"\n'
        '  }\n'
        '}\n'
    ),
    # base/harnesses/codex/harness.json
    'sha256:4fda0752363d2066d7555a8219e35b68ebe17414254d67504edc93eecfcac1e0': (
        '{\n'
        '  "schema": 1,\n'
        '  "description": "Codex CLI; prompt-led coordination and explicit native session resume.",'
        '\n'
        '  "checked_cli_version": "0.154.0",\n'
        '  "session": "reported",\n'
        '  "spawn": ["codex", "--cd", "{workspace}", "--add-dir", "{agent_dir}", "-c", "developer_i'
        'nstructions={bootstrap_json}", "--", "{task}"],\n'
        '  "resume": ["codex", "resume", "--cd", "{workspace}", "--add-dir", "{agent_dir}", "-c", "'
        'developer_instructions={bootstrap_json}", "--", "{session_id}"],\n'
        '  "model_args": ["--model", "{model}"],\n'
        '  "unset_env": ["CODEX_THREAD_ID", "CLAUDECODE"],\n'
        '  "bootstrap": "At startup, first run `\\"$MV2_CLI\\" session-bind --session-id \\"$CODEX_THR'
        'EAD_ID\\"` to record this native session for resume. If the variable is absent, report that'
        '; never guess another session\'s ID.",\n'
        '  "docs": "https://developers.openai.com/codex/cli/reference/"\n'
        '}\n'
    ),
    # base/harnesses/codex/harness.json
    'sha256:7f9ec98c85f2c37e93f0dedbf25c28bfa0a8c81b40c07b22c6578653a84cf725': (
        '{\n'
        '  "schema": 1,\n'
        '  "description": "Codex CLI; prompt-led coordination and explicit native session resume.",'
        '\n'
        '  "checked_cli_version": "0.154.0",\n'
        '  "session": "reported",\n'
        '  "spawn": ["codex", "--cd", "{workspace}", "--add-dir", "{agent_dir}", "-c", "developer_i'
        'nstructions={bootstrap_json}", "--", "{task}"],\n'
        '  "resume": ["codex", "resume", "--cd", "{workspace}", "--add-dir", "{agent_dir}", "-c", "'
        'developer_instructions={bootstrap_json}", "--", "{session_id}"],\n'
        '  "model_args": ["--model", "{model}"],\n'
        '  "unset_env": ["CODEX_THREAD_ID"],\n'
        '  "bootstrap": "At startup, first run `\\"$MV2_CLI\\" session-bind --session-id \\"$CODEX_THR'
        'EAD_ID\\"` to record this native session for resume. If the variable is absent, report that'
        '; never guess another session\'s ID.",\n'
        '  "docs": "https://developers.openai.com/codex/cli/reference/"\n'
        '}\n'
    ),
    # base/harnesses/codex/harness.json
    'sha256:a1dbe54c8b02dfeea506e05265e201638a3d63ec69f3aa691f6ffdbb2b5d5c9a': (
        '{\n'
        '  "schema": 2,\n'
        '  "adapter": "codex",\n'
        '  "description": "Codex CLI; prompt-led coordination and explicit native session resume.",'
        '\n'
        '  "settings": {}\n'
        '}\n'
    ),
    # base/harnesses/pi/harness.json
    'sha256:14703ae91bf78f574e544116723ec7c73ef4d34ac8dfc0eae0ce3a2d53752d71': (
        '{\n'
        '  "schema": 2,\n'
        '  "adapter": "pi",\n'
        '  "description": "Pi interactive sessions; optional extension for automatic inbox delivery'
        '.",\n'
        '  "settings": {}\n'
        '}\n'
    ),
    # base/harnesses/pi/harness.json
    'sha256:7584fc9f61fcfb2de679d40e4217677c78675feae778b91765868146ed2724e0': (
        '{\n'
        '  "schema": 1,\n'
        '  "description": "Pi interactive sessions; optional extension for automatic inbox delivery'
        '.",\n'
        '  "tested_version": "0.84.3",\n'
        '  "spawn": ["pi", "--name", "{name}", "--session", "{session_file}", "--append-system-prom'
        'pt", "{bootstrap_file}", "@{task_file}"],\n'
        '  "resume": ["pi", "--name", "{name}", "--session", "{session_file}", "--append-system-pro'
        'mpt", "{bootstrap_file}"],\n'
        '  "model_args": ["--model", "{model}"],\n'
        '  "unset_env": ["CODEX_THREAD_ID", "CLAUDECODE"],\n'
        '  "docs": "https://github.com/earendil-works/pi/tree/main/packages/coding-agent"\n'
        '}\n'
    ),
    # base/harnesses/pi/harness.json
    'sha256:daae1423289e475c82909bfa179d64271efd68fbee685443d11f409b5f67ac0c': (
        '{\n'
        '  "schema": 1,\n'
        '  "description": "Pi interactive sessions; optional extension for automatic inbox delivery'
        '.",\n'
        '  "tested_version": "0.84.3",\n'
        '  "spawn": ["pi", "--name", "{name}", "--session", "{session_file}", "--append-system-prom'
        'pt", "{bootstrap_file}", "@{task_file}"],\n'
        '  "resume": ["pi", "--name", "{name}", "--session", "{session_file}", "--append-system-pro'
        'mpt", "{bootstrap_file}"],\n'
        '  "model_args": ["--model", "{model}"],\n'
        '  "docs": "https://github.com/earendil-works/pi/tree/main/packages/coding-agent"\n'
        '}\n'
    ),
    # development/harnesses/codex/harness.json
    'sha256:fb3ff45976e48d976aed5f3a7c3077b1ca80932539bb945d8748001843fe84e5': (
        '{\n'
        '  "schema": 2,\n'
        '  "adapter": "codex",\n'
        '  "description": "Codex CLI; prompt-led coordination and explicit native session resume.",'
        '\n'
        '  "settings": {\n'
        '    "inbox_bridge": true\n'
        '  }\n'
        '}\n'
    ),
}
