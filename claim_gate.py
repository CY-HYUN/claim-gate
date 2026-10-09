#!/usr/bin/env python3
"""claim-gate: a Claude Code Stop hook that asks for evidence before a claim ends the turn.

Claude Code runs this script when the assistant is about to stop. It reads the hook's JSON from
stdin, takes the last assistant message, and looks for five kinds of claim that agents tend to
make without proof:

  completion  "all tests pass now", "fixed"                with no command shown
  number      "found 42 files", "accuracy is 87%"          with no command that produced it
  absence     "there is no caller", "nothing uses this"    after fewer than two sources
  universal   "every file", "all tests", "none of the calls" over a set never counted
  correction  "correction: it is 12, not 9"                a new number or cause, unchecked

When one fires, it prints {"decision": "block", "reason": ...}. Claude Code then keeps the turn
going and shows the reason to the model, which runs the check or softens the claim. A second
stop in the same turn (stop_hook_active) always passes, so the gate can never loop.

Config: claim_gate.json next to this file, or the path in CLAIM_GATE_CONFIG. Every key is
optional; see claim_gate.example.json. Any internal error passes the stop and is logged to
stderr: a broken guard must not lock the session.
"""
from __future__ import annotations

import json
import os
import re
import sys

I = re.IGNORECASE

# ---- evidence: what the reply shows, read from the raw text --------------------------------------

FENCED_RE = re.compile(r"```.*?```|~~~.*?~~~", re.S)
# An inline span with a space in it reads as a command (`pytest -q`, `grep -rn foo src`);
# a bare identifier (`foo.py`) does not.
INLINE_CMD_RE = re.compile(r"`[^`\n]*\S \S[^`\n]*`")
PROMPT_LINE_RE = re.compile(r"(?m)^\s*\$ \S")
CITED_RE = re.compile(r"[\w./\\-]+\.[A-Za-z]{1,5}:\d+")


def evidence_count(raw: str) -> int:
    """Distinct pieces of shown evidence: code blocks, command spans, `$ cmd` lines, file:line."""
    rest = FENCED_RE.sub(" ", raw)
    return (len(FENCED_RE.findall(raw)) + len(INLINE_CMD_RE.findall(rest))
            + len(PROMPT_LINE_RE.findall(rest)) + len(CITED_RE.findall(rest)))


# ---- prose: what the reply claims, read with code and quotes removed -----------------------------

NOT_PROSE = [re.compile(p, re.S | re.M) for p in (
    r"```.*?```", r"~~~.*?~~~", r"`[^`\n]*`", r"^\s*>.*$", r"https?://\S+")]


def prose(raw: str) -> str:
    """The reply with code, quotes and links blanked, so a quoted 'done' does not count."""
    for p in NOT_PROSE:
        raw = p.sub(" ", raw)
    return raw


def sentences(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.!?])\s+|\n+", text) if s.strip()]


# A plan, request or question is not a claim: "I'll check every file", "can you run all tests?"
PLAN_RE = re.compile(
    r"\b(i'?ll|i will|we'?ll|we will|let'?s|let me|going to|plan to|need to|should|todo|"
    r"please|can you|could you|make sure|once|if|when|after)\b", I)


def is_claim(sentence: str) -> bool:
    return not sentence.rstrip().endswith("?") and not PLAN_RE.search(sentence)


# ---- the five rules ------------------------------------------------------------------------------

COMPLETION_RE = re.compile(
    r"\b(all done|(is|are|now|it'?s|that'?s|everything'?s|everything is)\s+(done|complete|finished|"
    r"fixed|working)|(i'?ve|i have|we'?ve|we have|successfully)\s+(completed|finished|fixed|"
    r"implemented|resolved)|(tests?|build|ci|checks?)\s+(now\s+)?(pass(es|ed)?\b|(is|are)\s+"
    r"(green|passing))|works now|ready to (merge|ship|deploy))", I)
COMPLETION_DENIED_RE = re.compile(r"\b(not|n't|never|isn'?t|aren'?t|yet)\b", I)

UNIT = (r"files?|lines?|rows?|records?|hits?|matches|tests?|errors?|warnings?|failures?|"
        r"occurrences|entries|items|calls?|callers?|users?|requests?|bugs?|issues?|commits?|samples?")
COUNT_RE = re.compile(rf"\b\d[\d,]*(\.\d+)?\s*(%|\b({UNIT})\b)|\b\d+\s*/\s*\d+\b", I)
OBSERVED_RE = re.compile(
    r"\b(found|counted|there (are|were|is)|shows?|showed|contains?|returned|matched|reported|"
    r"measured|got|have|has|had|passed|failed|failing|scored|is|are|was|were)\b", I)
CAPACITY_RE = re.compile(r"\b(can|could|up to|at most|at least|e\.g\.|for example|per)\b", I)

ABSENCE_RE = re.compile(
    r"\b(there (is|are|was|were) no|no \w+(\s\w+)? (exists?|found|uses?|calls?)|"
    r"(does|do|did)( not|n'?t) exist|not found|(couldn'?t|could not|cannot|can'?t) find|"
    r"nothing (uses|calls|references|imports|reads)|nobody (uses|calls)|"
    r"(is|are) (never|not) (used|called|referenced|imported|read)|unused|dead code)\b", I)

QUANTIFIER_RE = re.compile(
    r"(?<!\bat )(?<!\bafter )\b(all|every|none|always|never)\b(?! right\b)", I)
NOUN_RE = re.compile(
    r"\b(files?|tests?|calls?|callers?|functions?|modules?|rows?|records?|cases?|endpoints?|"
    r"places?|usages?|references?|items?|runs?|commits?|paths?|branches?|configs?|services?)\b", I)
# A counted scope is fine: "all 12 files", "12/12 tests", "9 of 9 callers", "both".
ENUMERATED_RE = re.compile(
    r"\b(all|every|each|none of the)\s+\d+\b|\b\d+\s*(/|of|out of)\s*\d+\b|\bboth\b|\bnot (all|every)\b", I)

CORRECTION_RE = re.compile(
    r"\b(correction|i was wrong|i made a mistake|my mistake|i misread|i miscounted|i retract|"
    r"that was wrong|scratch that|to correct (that|myself|my earlier))\b", I)
FRESH_RE = re.compile(
    rf"\d+\s*/\s*\d+|\d+(\.\d+)?\s*%|\b\d[\d,]*\s*({UNIT})\b|\b\d+\.\d+\b|"
    r"\b(because|due to|caused by|the (root )?cause|the bug is|the reason is)\b", I)


def fires_completion(text: str) -> bool:
    return any(COMPLETION_RE.search(s) and is_claim(s) and not COMPLETION_DENIED_RE.search(s)
               for s in sentences(text))


def fires_number(text: str) -> bool:
    return any(COUNT_RE.search(s) and OBSERVED_RE.search(s) and is_claim(s)
               and not CAPACITY_RE.search(s) for s in sentences(text))


def fires_absence(text: str) -> bool:
    return any(ABSENCE_RE.search(s) and is_claim(s) for s in sentences(text))


def fires_universal(text: str) -> bool:
    return any(QUANTIFIER_RE.search(s) and NOUN_RE.search(s) and is_claim(s)
               and not ENUMERATED_RE.search(s) for s in sentences(text))


def fires_correction(text: str) -> bool:
    return bool(CORRECTION_RE.search(text) and FRESH_RE.search(text))


# name -> (detector, default marker, default evidence needed, the instruction shown to the model)
RULES = {
    "completion": (fires_completion, "Verified:", 1,
        "This reply says the work is done but shows no check. Run the check that proves it (the "
        "tests, the script, the build) and add a 'Verified:' line with the exact command and its "
        "result line. If you cannot run it, replace the claim with what is done and what is not "
        "verified."),
    "number": (fires_number, "Counted with:", 1,
        "This reply states a count or a metric with no command that produced it. For each "
        "number, add a 'Counted with:' line: the exact command or query (pasteable), what one "
        "unit is (line, file, distinct event), and the denominator. A number you cannot "
        "reproduce should be removed or labelled as an estimate."),
    "absence": (fires_absence, "Searched:", 2,
        "This reply says something does not exist or is unused, and shows fewer than two places "
        "searched. One grep is one layer: dynamic calls, config files, other repos, generated "
        "code and logs are others. Add a 'Searched:' block with at least two rows: place -> exact "
        "command -> result. If you only looked once, say 'not found in X' instead of 'none'."),
    "universal": (fires_universal, "Scope:", 0,
        "This reply says all / every / none about a set it never counted. Add a 'Scope:' line: "
        "how many items the sentence covers (M), how many you actually checked (N), and the "
        "names of any you did not. Or narrow the sentence to what you checked "
        "('all 12 files in src/' instead of 'all files')."),
    "correction": (fires_correction, "Correction source:", 1,
        "This reply corrects an earlier statement, and the correction brings a new number or "
        "cause. Corrections feel careful, so nobody checks them twice. Add a 'Correction "
        "source:' line: where you checked the new claim in this turn (file:line or the command "
        "and its output). If you did not check it, keep only what you observed."),
}

DEFAULTS = {"mode": "block", "min_reply_chars": 40}


def load_config(path: str | None = None) -> dict:
    """Defaults merged with the JSON file. A file that does not parse is reported, then ignored."""
    path = path or os.environ.get("CLAIM_GATE_CONFIG") or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "claim_gate.json")
    cfg = {**DEFAULTS, "rules": {}}
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as f:
                user = json.load(f)
        except (OSError, ValueError) as exc:
            print(f"claim-gate: ignoring config {path}: {exc}", file=sys.stderr)
            return cfg
        cfg.update({k: v for k, v in user.items() if k != "rules"})
        cfg["rules"] = user.get("rules") or {}
        for unknown in set(cfg["rules"]) - set(RULES):
            print(f"claim-gate: unknown rule '{unknown}' in {path}", file=sys.stderr)
    return cfg


def check(raw: str, cfg: dict | None = None) -> list[tuple[str, str]]:
    """(rule name, instruction) for every rule this reply trips, in RULES order."""
    cfg = cfg if cfg is not None else load_config()
    if len(raw.strip()) < int(cfg.get("min_reply_chars", 0)):
        return []
    text, shown = prose(raw), evidence_count(raw)
    found = []
    for name, (detect, marker, need, ask) in RULES.items():
        rc = cfg.get("rules", {}).get(name, {})
        if rc.get("enabled", True) is False:
            continue
        extra = [re.compile(p, I) for p in rc.get("extra_triggers", [])]
        hit = detect(text) or any(p.search(text) for p in extra)
        marker = rc.get("marker", marker)
        need = int(rc.get("min_evidence", need))
        # universal has need 0: only its marker or a narrowed sentence clears it
        satisfied = marker in raw or (need > 0 and shown >= need)
        if hit and not satisfied:
            found.append((name, ask.replace(f"'{RULES[name][1]}'", f"'{marker}'")))
    return found


# ---- transcript fallback, for Claude Code versions without last_assistant_message ---------------

TAIL_BYTES = 8 * 2**20


def last_assistant_text(transcript_path: str) -> str:
    """Assistant text since the last real user message. Tool results arrive as 'user' rows and do
    not reset the turn, so a long answer followed by a tool call and a one-line sign-off is read
    whole."""
    parts: list[str] = []
    with open(transcript_path, "rb") as f:
        if f.seek(0, os.SEEK_END) > TAIL_BYTES:
            f.seek(-TAIL_BYTES, os.SEEK_END)
            f.readline()  # drop the cut line
        else:
            f.seek(0)
        for line in f:
            try:
                row = json.loads(line.decode("utf-8", errors="replace"))
            except ValueError:
                continue
            content = (row.get("message") or {}).get("content") or []
            if row.get("type") == "user":
                if isinstance(content, str) or not any(
                        isinstance(c, dict) and c.get("type") == "tool_result" for c in content):
                    parts = []
            elif row.get("type") == "assistant" and isinstance(content, list):
                parts += [c.get("text", "") for c in content
                          if isinstance(c, dict) and c.get("type") == "text"]
    return "\n".join(p for p in parts if p)


def main() -> int:
    try:
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8-sig") or "{}")
        if payload.get("stop_hook_active"):
            return 0  # already blocked once this turn: never loop
        text = payload.get("last_assistant_message")
        if text is None and payload.get("transcript_path"):
            text = last_assistant_text(payload["transcript_path"])
        cfg = load_config()
        findings = check(text or "", cfg)
    except Exception as exc:  # fail open, but say so
        print(f"claim-gate: skipped, internal error: {exc!r}", file=sys.stderr)
        return 0
    if not findings:
        return 0
    head = (f"{len(findings)} claims in this reply need evidence. Address all of them.\n\n"
            if len(findings) > 1 else "")
    reason = head + "\n\n".join(f"[{name}] {ask}" for name, ask in findings)
    if cfg.get("mode") == "warn":
        print(json.dumps({"systemMessage": "claim-gate: " + reason}))
    else:
        print(json.dumps({"decision": "block", "reason": reason}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
