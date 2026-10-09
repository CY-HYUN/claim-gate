# claim-gate

A Claude Code **Stop hook** that stops the assistant from ending its turn on a claim it has not backed up.
When a reply says "all tests pass", "found 42 files" or "nothing calls this" without showing the command
that proves it, the hook blocks the stop and tells the model which evidence to add.

One Python file, standard library only, about 270 lines.

## Why

LLM coding agents over-claim. They write "fixed" before running the tests, quote a count they never
counted, call a function unused after one grep, and say "every endpoint" about endpoints they never
listed. The claim reads as finished work, so the reader stops checking.

This gate turns "show your evidence" from a habit into a rule. It does not judge whether a claim is
true. It checks whether the reply shows how the claim was made, and if not, the model gets one more
round to run the check or soften the sentence.

## Quickstart

```bash
git clone https://github.com/CY-HYUN/claim-gate && cd claim-gate
echo '{"last_assistant_message": "Fixed the date parser. I found 42 files that call it and all tests pass now."}' | python claim_gate.py
```

Output (shortened):

```json
{"decision": "block", "reason": "3 claims in this reply need evidence. Address all of them.\n\n[completion] This reply says the work is done but shows no check. Run the check that proves it ... add a 'Verified:' line with the exact command and its result line. ...\n\n[number] This reply states a count or a metric with no command that produced it. ...\n\n[universal] This reply says all / every / none about a set it never counted. ..."}
```

The same reply with its evidence passes (no output, exit 0):

```text
Fixed the date parser. Verified: `python -m pytest -q` printed 31 passed. Scope: 31 of 31 tests in tests/.
```

## The five rules

| Rule | Fires on | Cleared by |
|---|---|---|
| `completion` | "all done", "is fixed", "tests pass", "ready to merge" | a code block or command (`` `pytest -q` ``), or a `Verified:` line |
| `number` | a count or percentage stated as observed: "found 42 files", "accuracy is 87%" | the command that produced it, or a `Counted with:` line |
| `absence` | "there is no caller", "nothing uses this", "unused", "could not find" | **two** pieces of evidence (two commands, or file:line citations), or a `Searched:` block |
| `universal` | all / every / none / always / never next to a countable noun | a counted scope ("all 6 endpoints", "9 of 12 files"), or a `Scope:` line |
| `correction` | "correction", "I was wrong", "my mistake" together with a new number or cause | a command or file:line in the reply, or a `Correction source:` line |

Plans and questions do not fire ("I'll check every file", "can you run the tests?"), and text inside
code blocks, inline code and `>` quotes is ignored, so quoting a log does not trip the gate.

When the hook blocks, Claude Code sets `stop_hook_active` on the next stop of the same turn, and the
hook always passes then. It can block at most once per turn and never loops.

## Install

1. Copy `claim_gate.py` into your project, for example `.claude/hooks/claim_gate.py`.
2. Add this to `.claude/settings.json` (project) or `~/.claude/settings.json` (all projects):

```json
{
  "hooks": {
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "python \"$CLAUDE_PROJECT_DIR/.claude/hooks/claim_gate.py\"",
            "timeout": 30
          }
        ]
      }
    ]
  }
}
```

Use `python3` instead of `python` if that is the name on your system. The hook reads
`last_assistant_message` from the Stop event; on Claude Code versions that do not send it, it reads the
last assistant turn from `transcript_path` (only the last 8 MB of the file).

## Configuration

Optional. Put a `claim_gate.json` next to the script, or point `CLAIM_GATE_CONFIG` at one.
`claim_gate.example.json` lists every key:

| Key | Default | Meaning |
|---|---|---|
| `mode` | `"block"` | `"warn"` shows the finding to the user (`systemMessage`) and never blocks |
| `min_reply_chars` | `40` | shorter replies are not checked |
| `rules.<name>.enabled` | `true` | turn one rule off |
| `rules.<name>.marker` | see table | the line that clears the rule, e.g. `"Proof:"` |
| `rules.<name>.min_evidence` | 1 (`absence`: 2) | how many commands or file:line citations clear the rule |
| `rules.<name>.extra_triggers` | `[]` | more regexes that fire the rule, e.g. phrases in your own language |

A config file that does not parse is reported on stderr and the defaults are used. Any internal error
lets the stop through and prints the error to stderr: a broken guard should not lock your session.

## Tests

```bash
python tests/test_claim_gate.py     # no dependencies
python -m pytest -q                 # same tests, if pytest is installed
```

17 tests. Each rule has a reply that must fire, a reply with evidence that must pass, and a claim-free
reply its detector must ignore. The end-to-end tests run the script the way Claude Code does (stdin
JSON in, JSON decision out). To check that the tests can fail, each detector was replaced with
"always fires" and "never fires" in turn; all 10 broken versions turned at least one test red.

## Limits

- **It checks for evidence, not for truth.** A reply can paste a command it never ran. The gate makes
  the claim checkable by a reader; it does not check it.
- **English only, regex based.** It misses claims phrased in ways the patterns do not cover, and it
  sometimes fires on descriptive sentences ("all functions in this module are pure"). Use
  `extra_triggers` for other languages, `enabled: false` for a rule that is noisy for your work, or
  `mode: "warn"` while you tune it.
- **Evidence anywhere in the reply counts.** One command clears every `number` claim in the same reply,
  even claims it did not produce.

## Origin

Adapted from a personal Claude Code setup in which this gate grew to about 40 rules over several
months of daily use, each added after a real over-claim. This repository keeps the five rules that are
not tied to one workplace or one language.

## License

MIT, see [LICENSE](LICENSE).
