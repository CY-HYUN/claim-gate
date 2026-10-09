"""Each rule is shown to fire on one reply and to stay quiet on a close neighbour.

    python -m pytest -q          # if pytest is installed
    python tests/test_claim_gate.py   # no dependencies
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import claim_gate as cg  # noqa: E402

CFG = {"mode": "block", "min_reply_chars": 0, "rules": {}}

# rule -> (reply that must fire, reply that must not)
CASES = {
    "completion": (
        "I updated the parser and all tests pass now.",
        "I updated the parser. Verified: `python -m pytest -q` -> 14 passed in 0.8s.",
    ),
    "number": (
        "I found 42 files that import the old client.",
        "I found 42 files that import the old client: `grep -rl old_client src | wc -l` printed 42.",
    ),
    "absence": (
        "There is no caller of parse_legacy, so it is safe to delete.",
        "Searched: `grep -rn parse_legacy src` -> 0 hits; `grep -rn parse_legacy config` -> 0 hits. "
        "There is no caller of parse_legacy in either place.",
    ),
    "universal": (
        "Every endpoint now validates the token.",
        "All 6 endpoints in routes.py now validate the token.",
    ),
    "correction": (
        "Correction: the failure rate is 12%, not 9%.",
        "Correction: the failure rate is 12%, not 9% (`python stats.py --failures` printed 0.12).",
    ),
}


# rule -> a reply with no evidence that the detector itself must not read as that claim
CLEAN = {
    "completion": "I updated the parser; the tests have not been run yet.",
    "number": "The parser can handle up to 500 files per run.",
    "absence": "I will search for callers of parse_legacy next.",
    "universal": "The function returns a list of tuples; each tuple is (name, reason).",
    "correction": "Correction: I meant the parser module, not the lexer.",
}


def names(text, cfg=CFG):
    return [n for n, _ in cg.check(text, cfg)]


def test_each_rule_fires_on_its_example():
    for rule, (fire, _) in CASES.items():
        assert rule in names(fire), f"{rule} did not fire on: {fire}"


def test_each_rule_stays_quiet_on_its_neighbour():
    for rule, (_, quiet) in CASES.items():
        assert rule not in names(quiet), f"{rule} fired on: {quiet}"


def test_each_detector_ignores_a_claim_free_reply():
    for rule, text in CLEAN.items():
        assert rule not in names(text), f"{rule} fired on: {text}"


def test_plans_and_questions_are_not_claims():
    for text in ("I'll check every file and then run all tests.",
                 "Can you confirm the build is green?",
                 "Once tests pass, it is ready to merge."):
        assert names(text) == [], (text, names(text))


def test_claims_inside_code_and_quotes_do_not_fire():
    text = "Here is the log:\n```\nall tests passed\nfound 42 files\n```\n> Every endpoint is done."
    assert names(text) == []


def test_negated_completion_does_not_fire():
    assert "completion" not in names("The migration is not done yet; two tables remain.")


def test_absence_needs_two_sources_not_one():
    one = "I ran `grep -rn parse_legacy src` and there is no caller."
    assert "absence" in names(one)


def test_universal_marker_clears_it_but_a_command_alone_does_not():
    claim = "Every endpoint now validates the token, checked with `grep -c validate routes.py`."
    assert "universal" in names(claim)
    assert "universal" not in names(claim + " Scope: 6 of 6 endpoints in routes.py.")


def test_config_disables_a_rule_and_renames_a_marker():
    off = {**CFG, "rules": {"completion": {"enabled": False}}}
    assert "completion" not in names(CASES["completion"][0], off)
    renamed = {**CFG, "rules": {"completion": {"marker": "Proof:"}}}
    found = dict(cg.check(CASES["completion"][0], renamed))
    assert "'Proof:'" in found["completion"]
    assert "completion" not in names(CASES["completion"][0] + " Proof: ran it by hand.", renamed)


def test_extra_triggers_extend_a_rule():
    cfg = {**CFG, "rules": {"completion": {"extra_triggers": [r"\bc'est fini\b"]}}}
    assert "completion" in names("Le refactoring, c'est fini.", cfg)
    assert "completion" not in names("Le refactoring, c'est fini.")


def test_min_reply_chars_skips_short_replies():
    assert names("Fixed, tests pass now.", {**CFG, "min_reply_chars": 40}) == []


# ---- end to end: the script as Claude Code runs it -----------------------------------------------

def run(payload, config=None):
    with tempfile.TemporaryDirectory() as d:
        cfg_path = Path(d) / "claim_gate.json"
        if config is not None:
            cfg_path.write_text(config, encoding="utf-8")
        env = {**os.environ, "CLAIM_GATE_CONFIG": str(cfg_path)}
        p = subprocess.run([sys.executable, str(ROOT / "claim_gate.py")], input=json.dumps(payload),
                           capture_output=True, text=True, env=env, timeout=30)
    assert p.returncode == 0, p.stderr
    return (json.loads(p.stdout) if p.stdout.strip() else None), p.stderr


def test_hook_blocks_with_a_reason():
    out, _ = run({"last_assistant_message": CASES["completion"][0] + " " + CASES["number"][0]})
    assert out["decision"] == "block"
    assert "[completion]" in out["reason"] and "[number]" in out["reason"]


def test_hook_never_blocks_twice_in_a_turn():
    out, _ = run({"stop_hook_active": True, "last_assistant_message": CASES["completion"][0]})
    assert out is None


def test_hook_reads_the_transcript_when_the_message_field_is_absent():
    rows = [
        {"type": "user", "message": {"content": "fix the parser"}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "I rewrote the tokenizer loop."}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "content": "ok"}]}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "The build is green, ready to merge."}]}},
    ]
    with tempfile.TemporaryDirectory() as d:
        tp = Path(d) / "t.jsonl"
        tp.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
        assert cg.last_assistant_text(str(tp)) == (
            "I rewrote the tokenizer loop.\nThe build is green, ready to merge.")
        out, _ = run({"transcript_path": str(tp)})
    assert out["decision"] == "block"


def test_warn_mode_shows_a_message_and_does_not_block():
    out, _ = run({"last_assistant_message": CASES["completion"][0]}, config='{"mode": "warn"}')
    assert "decision" not in out and out["systemMessage"].startswith("claim-gate:")


def test_broken_config_is_reported_and_defaults_still_apply():
    out, err = run({"last_assistant_message": CASES["completion"][0]}, config="{not json")
    assert "ignoring config" in err and out["decision"] == "block"


def test_internal_error_fails_open_and_says_so():
    out, err = run({"transcript_path": os.path.join(tempfile.gettempdir(), "no", "such.jsonl")})
    assert out is None and "internal error" in err


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    failed = 0
    for n, f in tests:
        try:
            f()
            print(f"PASS  {n}")
        except Exception as exc:
            failed += 1
            print(f"FAIL  {n}  {exc}")
    print(f"{len(tests) - failed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
