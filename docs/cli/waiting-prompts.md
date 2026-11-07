# Answer a waiting agent

When an agent's status is `waiting`, it is stopped at a prompt in its terminal: a permission prompt, a question it asked you, a plan to approve, or a form. `mv2 answer` shows that prompt with the choices the harness offers, and answers it through the harness's own answer path. The agent gets what it would get if you had answered in its terminal. It works for agents whose harness supports it, with a live connection; `mv2 capabilities --agent NAME` says `answer: unavailable` and why for the others.

```sh
mv2 answer worker                                  # the prompts it waits on, each with its ID
mv2 answer worker ID --choice allow
mv2 answer worker ID --choice deny --message 'Use the staging database instead'
mv2 answer worker ID --answer 1=Postgres --answer 2=Yes
mv2 answer worker --result ID                      # an earlier answer's outcome
```

Only a person, from a terminal that isn't an agent's, or a plugin can read or answer prompts. An agent can't: ask the person instead. Answering works while the agent is [focused](messaging.md#focus-an-agent).

## Read the prompts

`mv2 answer NAME` lists every prompt the agent waits on now. Each line shows the prompt's ID, its kind, and what it is about, such as the command or file a permission prompt asks about. Under it are its choices, or its questions, numbered:

```text
4f2c9a1e  permission  Bash: rm -rf build
    choices: allow (Yes), allow_session (Yes, for this session), deny (No) [takes --message]
7b0d3e55  question  AskUserQuestion: Which database should I use?
    1. Database: Which database should I use?  [Postgres | SQLite]  (typed text allowed)
```

A choice marked `saves a rule` also changes your saved settings, such as always allowing a command, and only a person can give it. A prompt mv2 can't answer says why, often "answer it in the terminal". When mv2 can't tell whether it sees every prompt the agent waits on, the list ends with `Coverage unknown` and the reason.

A prompt can be answered only while it is open and the agent is `waiting`. mv2 never answers a prompt in advance or holds an answer for a later one.

## Answer

Give a choice for a permission prompt or a plan, with `--message` where the choice takes one. Give `--answer QUESTION=LABEL` for each question. QUESTION is the question's number from the list, its header or its text, and LABEL is one of its labels, or typed text where the question allows it. Repeat `--answer` for each label of a question that allows several. mv2 checks the whole answer against the prompt before it sends anything, and a refused answer sends nothing.

Some questions take a note along with their answer, such as Codex's "Other" option: the list marks them `takes a --note`. Give it with `--note QUESTION=TEXT`, once per question, beside the question's `--answer`. A question with no labels can be answered by its note alone.

The answer, its notes and their metadata share a 64 KiB limit. An oversized answer is refused before an attempt is recorded; shorten it and answer the same prompt again.

Give a secret answer, such as a password a form asks for, with `--stdin` as JSON:

```sh
mv2 answer worker ID --stdin <<'EOF'
{"answers": {"Password": "..."}, "notes": {"Reason": "..."}}
EOF
```

`mv2 answer` refuses a secret answer or note on the command line, where your shell history and the process list would keep it. mv2 never shows a secret answer back or records it.

Before it sends an answer given on the command line, `mv2 answer` reads the prompt to find its questions and to check that none is secret. If that prompt isn't among the ones it can read, it sends nothing and says what became of it: the person answered or refused it in the terminal first, it closed, the agent isn't running, or mv2 already answered it. Only when mv2 can't tell does it say to read the prompts again or to use `--stdin`. An error never quotes the answer or note you typed.

## Read the outcome

The answer waits up to 30 seconds for its outcome; change that with `--timeout`, or give `--timeout 0` to return as soon as it is sent.

| Outcome | Means |
| --- | --- |
| Answered | mv2's answer took. It says when the choice was one that saves a rule to your settings, which the harness then writes |
| The person answered first | Someone answered in the terminal before mv2's answer arrived. It says what they chose when the harness shows it |
| The prompt closed | The prompt closed another way: Esc, the turn ended, or the agent stopped |
| Sent first | mv2's answer went to the agent ahead of any answer from its terminal, but the harness doesn't confirm it took it (Codex) |
| Sent, outcome not known yet | Read it later with `--result ID` |
| Can't tell which answer took | Sent, but mv2 can't say whether it or the person's answer took |
| Not sent | The answer was refused, and the reason says why |

When an answer didn't take, the result says why: as fact when mv2 knows, such as the person answering first, the turn ending, or the agent waiting on another prompt by now (then `mv2 answer NAME` shows that one), and as possible explanations when it doesn't. It never tells you to answer again after an answer was sent.

The command exits 1 unless the answer took, went first, or is still pending. Each prompt takes one answer: mv2 never sends an answer twice, and `--result` only reads. For a prompt mv2 sent no answer to, `--result` says what became of it, or that it is still open. `--json` prints the whole result.

Each answer is recorded in the agent's `prompts/` folder: who answered, the prompt's kind and tool, the choice and the labels chosen, and the outcome, never a secret answer.
