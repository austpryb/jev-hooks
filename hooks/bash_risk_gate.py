#!/usr/bin/env python3
"""PreToolUse hook (matcher: Bash). One Jev call decides allow / ask / deny.

Deterministic first: a command that is plainly read-only never reaches Jev.
Then one request with four independent questions over {command, description, cwd}.
Fails open: no key, timeout, 429/529 -> exit 0 with no output, so the normal
permission flow decides exactly as it would without this hook.

Output when it has an opinion (PreToolUse JSON contract):
  {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                          "permissionDecision": "deny"|"ask",
                          "permissionDecisionReason": "..."}}
JEV_HOOKS_GATE_MODE=warn downgrades every deny to ask.
"""
import json, os, re, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))
import jev

READ_ONLY = re.compile(
    r"^\s*(ls|cat|head|tail|wc|pwd|echo|printf|which|type|date|stat|file|tree|du|df|"
    r"grep|rg|ugrep|find|fd|sed -n|uniq|cut|tr|jq|yq|diff|"
    r"git (status|log|diff|show|rev-parse|ls-files|blame|describe|fetch)|"
    r"git branch(?!.*\s-[dDmM]\b)|"                                # listing only; -d/-D/-m/-M mutate
    r"git remote(?!\s+(add|remove|rm|rename|set-url|prune|update)\b)|"
    r"gh (pr|issue|run|repo) (list|view|status|checks)|"
    r"go (build|vet|list|version|env)|go test(?!.*-exec\b)|npm (test|run (build|test|check|verify|typecheck))|"
    r"docker (ps|images|logs)|kubectl (get|describe|logs)|helm (template|list|status))\b"
)
# Anything here means the command can write, run something else, or hide a
# command, so the fast path is refused and Jev reads it (audit 2026-09-19: every
# one of these bypassed the old first-word check).
UNSAFE = re.compile(r"\$\(|`|-delete\b|-exec\b|-ok\b|system\(|\bxargs\b|\btee\b|\s-o\s")
# Redirections that only discard or merge streams are fine; any other `>` writes a file.
HARMLESS_REDIRECT = re.compile(r"2>&1|&>\s*/dev/null|[12]?>{1,2}\s*/dev/null")


def is_read_only(cmd):
    """True only when EVERY segment starts with a read-only program and nothing
    in the command can write, delete, or run a hidden command."""
    if UNSAFE.search(cmd):
        return False
    if ">" in HARMLESS_REDIRECT.sub("", cmd):
        return False
    parts = re.split(r"\|\||&&|;|\||\n", cmd)
    return all(READ_ONLY.match(p) for p in parts if p.strip())


def main():
    inp = jev.read_stdin()
    if inp.get("tool_name") != "Bash":
        return
    ti = inp.get("tool_input") or {}
    cmd = ti.get("command") or ""
    if not cmd.strip():
        return
    if is_read_only(cmd):
        if os.environ.get("JEV_HOOKS_DEBUG"):
            sys.stderr.write("fast-path\n")
        return
    state = {"command": cmd, "description": ti.get("description") or "", "cwd": inp.get("cwd") or ""}
    q = {
        "irreversible": jev.noul(
            "Would running `command` delete, overwrite or destroy data, kill processes, push or force-push to a remote, "
            "or change system or cloud state in a way a follow-up command cannot undo?",
            true="rm -rf, git push --force, git reset --hard, DROP TABLE, kubectl delete, terraform apply/destroy, "
                 "helm uninstall, overwriting a file with > when the file is not a scratch file, killing processes",
            false="builds, tests, reads, listing, creating a new file, git commit on a branch, writing under /tmp or a scratchpad"),
        "self_kill": jev.noul(
            "Could `command` terminate the shell or agent session that is running it? Consider pkill/killall/kill with a "
            "pattern or -f flag broad enough to match the current shell, the test runner, or a parent process.",
            true="pkill -f <substring that appears in the hook's own command line>, killall bash, kill -9 -1",
            false="kill <one specific pid>, pgrep, pkill -f with a character-class pattern like 'db[.]test' that cannot match itself"),
        "scope": jev.choice(
            "Does `command` affect exactly the files, processes or resources it names, or a pattern or wildcard that could "
            "match more than the `description` intends?",
            {"named": "explicit paths, one pid, one resource name", "pattern": "globs, -r over a directory, regex process match, wildcard resource selectors"}),
        "action": jev.choice(
            "Given `command` and `description`, what should a careful reviewer do before it runs?",
            {"allow": "safe or trivially reversible; running it is fine",
             "confirm": "reversible only with effort, or its blast radius depends on state the reviewer cannot see; a human should confirm",
             "block": "will very likely destroy work, escape the intended scope, or kill the session; do not run as written"}),
    }
    a = jev.ask(state, q)
    if not a:
        return
    irr = a["irreversible"]["noul"]; sk = a["self_kill"]["noul"]
    act = a["action"]; scope = a["scope"]["choice"]
    reasons = []
    decision = None
    if sk >= 0.7:
        decision = "deny"; reasons.append(f"could kill the running session (p={sk:.2f})")
    elif act["choice"] == "block" and act["confidence"] >= 0.6:
        decision = "deny"; reasons.append(f"reviewer verdict: block (confidence {act['confidence']:.2f})")
    elif irr >= 0.5 or act["choice"] == "confirm" or (scope == "pattern" and irr >= 0.3):
        decision = "ask"; reasons.append(f"irreversible p={irr:.2f}, scope={scope}, verdict={act['choice']}")
    if not decision:
        jev.record("bash_gate", "silent", a, command=cmd[:200])
        return
    if decision == "deny" and os.environ.get("JEV_HOOKS_GATE_MODE") == "warn":
        decision = "ask"
    hint = " Narrow the pattern (e.g. pgrep -f 'db[.]test') or name the exact target, then retry." if sk >= 0.7 or scope == "pattern" else ""
    jev.record("bash_gate", decision, a, command=cmd[:200])
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": decision,
                                              "permissionDecisionReason": "jev-hooks: " + "; ".join(reasons) + hint}}))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass  # a hook bug must not block a session
