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
import json, os, re, shlex, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))
import jev

# `cd` is here because it only moves the shell: 179 of 532 judged commands in the
# decision log (2026-09-21) were `cd <repo> && <something already read-only>`,
# each paying a Jev call and ~300 ms to be told it was harmless. A cd that
# hides a command (`cd $(...)`) is caught by UNSAFE before this is consulted.
READ_ONLY = re.compile(
    r"^\s*(cd|ls|cat|head|tail|wc|pwd|echo|printf|which|type|date|stat|file|tree|du|df|"
    r"grep|rg|ugrep|find|fd|sed -n|sort|uniq|cut|tr|jq|yq|diff|"
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
UNSAFE = re.compile(
    r"\$\(|`|[<>]\(|-delete\b|-exec\b|-execdir\b|-ok\b|-okdir\b|-fprint|-fls\b|system\(|\bxargs\b|\btee\b|"
    r"\s-o[\s=]|--output\b|-toolexec\b|-vettool\b|--pre\b|--upload-pack\b|--receive-pack\b|--post-renderer\b")
# Programs on the list above whose FLAGS turn them into writers or launchers.
# Audit 2026-09-21: 23 of 23 such commands took the silent fast path - among
# them `sed -n '1e rm -rf build'`, `find -execdir rm`, `git branch -f main
# HEAD~10`, `uniq in out`, and `cat <(touch x)`. A segment that trips any of
# these is sent to the judge instead; being wrong here costs one Jev call.
GIT_BRANCH_VALUE = {"--merged", "--no-merged", "--contains", "--no-contains", "--points-at", "--sort", "--format"}
GIT_BRANCH_LIST = {"-a", "-r", "-l", "-v", "-vv", "--all", "--remotes", "--list", "--show-current",
                   "--no-color", "--color", "--column", "--no-column", "-i", "--ignore-case"}
# Redirections that only discard or merge streams are fine; any other `>` writes a file.
HARMLESS_REDIRECT = re.compile(r"2>&1|&>\s*/dev/null|[12]?>{1,2}\s*/dev/null")

# Outward-facing: it publishes, merges or deploys where other people see it, and
# no local undo takes it back. These always ask, without a judgment call — the
# log showed `gh pr merge` scoring 0.38-0.44 irreversible, below any threshold
# worth setting, because nothing is destroyed on this machine. Anchored to the
# start of a command segment so a grep that merely MENTIONS `git push` is not a
# push. Matching here skips Jev entirely: one fewer call, and the prompt that
# matters least deserves to depend on a judge that is up.
# A program's global options may sit between it and the subcommand
# (`kubectl -n prod delete`, `git -C ../repo push`, `terraform -chdir=infra
# apply`): an option, optionally followed by one value.
_OPTS = r"(?:-\S+(?:\s+[^-\s|;&()]\S*)?\s+)*"
OUTWARD = re.compile(
    r"(?:^|[;&|(\n]\s*|\b(?:do|then|else)\s+)"          # a segment start, incl. a newline, a subshell, or a loop/if body
    r"(?:(?:sudo|time|env|npx|bunx|pnpm\s+dlx)\s+(?:\w+=\S*\s+)*)*"   # wrappers and VAR=val
    r"("
    r"gh\s+" + _OPTS + r"(?:pr\s+(?:merge|create)|release\s+(?:create|delete)|repo\s+(?:create|delete|edit|rename|archive))|"
    r"gh\s+api\b[^\n;&|]*\s(?:-X|--method)\s*['\"]?(?:POST|PUT|PATCH|DELETE)\b|"
    r"git\s+" + _OPTS + r"push(?![^\n;&|]*--dry-run)|"
    r"terraform\s+" + _OPTS + r"(?:apply|destroy)|"
    r"helm\s+" + _OPTS + r"(?:install|upgrade|uninstall)|"
    r"kubectl\s+" + _OPTS + r"(?:apply|delete)|"
    r"(?:npm|pnpm|yarn(?:\s+npm)?|bun)\s+publish|"
    r"docker\s+push|docker\s+(?:buildx\s+)?build\b[^\n;&|]*\s--push|"
    r"wrangler\s+(?:pages\s+)?(?:deploy|publish)|"
    r"make\s+" + _OPTS + r"roll-apply)\b")


def _tokens(seg):
    try:
        return shlex.split(seg)
    except ValueError:
        return None                    # unbalanced quotes: not provably anything


def _segment_read_only(seg):
    """One command segment: on the read-only list AND not one of the listed
    programs used with a flag that writes, deletes or launches."""
    if not READ_ONLY.match(seg):
        return False
    t = _tokens(HARMLESS_REDIRECT.sub(" ", seg))     # `2>/dev/null` is not an argument
    if t is None:
        return False
    prog = t[0] if t else ""
    if prog == "sed":
        # Only `sed -n '<address>p' file`: a single print command. A script that
        # does not END in p, holds a second command, or edits in place can write
        # (w), run a shell (e) or rewrite the file (-i).
        for x in t[1:]:
            if x.startswith(("--in-place", "--expression", "--file", "--separate")):
                return False
            if x.startswith("-") and not x.startswith("--") and set(x[1:].split(".")[0]) & set("iefs"):
                return False           # -i, -i.bak, -ni, -e, -f: edits in place or adds a command
        scripts = [x for x in t[1:] if not x.startswith("-")]
        if not scripts or not scripts[0].endswith("p") or ";" in scripts[0] or "\n" in scripts[0]:
            return False
    elif prog == "git" and len(t) > 1 and t[1] == "branch":
        want_value = False
        for x in t[2:]:
            if want_value:
                want_value = False; continue
            if x in GIT_BRANCH_VALUE:
                want_value = True; continue
            if any(x.startswith(v + "=") for v in GIT_BRANCH_VALUE) or x in GIT_BRANCH_LIST:
                continue
            return False               # -d/-D/-m/-M/-f/-c, a clustered -Df, or a NAME: it writes
    elif prog == "uniq":
        if len([x for x in t[1:] if not x.startswith("-")]) > 1:
            return False               # `uniq IN OUT` overwrites OUT
    elif prog == "fd":
        if any(x in ("-x", "-X", "--exec", "--exec-batch") for x in t[1:]):
            return False
    elif prog == "yq":
        if any(x in ("-i", "--inplace") or x.startswith("--inplace=") for x in t[1:]):
            return False
    elif prog == "go" and len(t) > 1 and t[1] == "env":
        if any(x in ("-w", "-u") for x in t[2:]):
            return False               # `go env -w` persists a setting
    return True


def split_segments(cmd):
    """Split on | || && ; newline and a bare & - but only OUTSIDE quotes. A
    regex alternation inside a quoted grep pattern (`grep "a\\|b"`) is not a
    pipe, and splitting there cut the pattern in half. A bare `&` backgrounds
    the left side and RUNS the right one; `&&`, `2>&1` and `&>` are not it."""
    out, cur, q, i, n = [], [], None, 0, len(cmd)
    while i < n:
        c = cmd[i]
        if q:
            cur.append(c)
            if c == "\\" and q == '"' and i + 1 < n:
                cur.append(cmd[i + 1]); i += 2; continue
            if c == q:
                q = None
            i += 1; continue
        if c == "\\" and i + 1 < n:
            cur.append(c); cur.append(cmd[i + 1]); i += 2; continue
        if c in "'\"":
            q = c; cur.append(c); i += 1; continue
        two = cmd[i:i + 2]
        if two in ("||", "&&"):
            out.append("".join(cur)); cur = []; i += 2; continue
        if c in "|;\n":
            out.append("".join(cur)); cur = []; i += 1; continue
        if c == "&" and not (i > 0 and cmd[i - 1] == ">") and not (i + 1 < n and cmd[i + 1] == ">"):
            out.append("".join(cur)); cur = []; i += 1; continue
        cur.append(c); i += 1
    out.append("".join(cur))
    return [x.strip() for x in out if x.strip()]


def is_read_only(cmd):
    """True only when EVERY segment is a read-only program used read-only, and
    nothing in the command can write, delete, or run a hidden command."""
    if UNSAFE.search(cmd):
        return False
    if ">" in HARMLESS_REDIRECT.sub("", cmd):
        return False
    return all(_segment_read_only(p) for p in split_segments(cmd))


# Heredoc BODIES are data, not commands: a commit message or a patch that has a
# line starting with `git push` is not a push. Found in the decision log: 7 outward
# prompts matched inside a heredoc. The body is dropped before matching; the
# line that opens the heredoc still counts.
_HEREDOC = re.compile(r"<<-?\s*(['\"]?)(\w+)\1[^\n]*\n.*?\n\s*\2\s*(?=\n|$)", re.S)


def strip_heredocs(cmd):
    return _HEREDOC.sub(lambda m: m.group(0).split("\n", 1)[0], cmd)


def outward_match(cmd):
    return OUTWARD.search(strip_heredocs(cmd))


def main():
    if jev.disabled("bash_gate"):
        return                       # switched off for this session (JEV_HOOKS_DISABLE)
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
    m = outward_match(cmd)
    if m:
        jev.record("bash_gate", "ask", None, command=cmd[:200], note="outward")
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "ask",
            "permissionDecisionReason": f"jev-hooks: `{m.group(1).strip()}` publishes, merges or deploys outside this "
                                        "machine, where no local undo reaches it. Confirm the target."}}))
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
    # A bare "confirm" is not enough to interrupt someone. Measured over 1042
    # logged gate decisions: `confirm` alone drove 256 of 259 prompts, most of
    # them on heredoc edits and scratchpad writes, while only 88 carried real
    # irreversibility — a prompt that fires on a third of all commands teaches
    # the one hand that reads it to approve without reading. So a confirm must
    # now come with confidence AND something actually at stake.
    # Raised 2026-09-25 from 0.5 / 0.3 / 0.3, replaying 5782 logged judgments:
    # 1044 prompts (18%), 553 of them heredoc file edits scoring 0.5-0.7 in git
    # repos, the coin-flip band; real hazards scored >= 0.8 (kill 0.93, terraform
    # 0.88, docker run 0.83). The new line prompts on 312 (5.4%) and keeps all 99
    # of those. What it drops outside heredocs was worktree churn, throwaway test
    # databases and deleting debug files; the two real outward actions among
    # them (gh api -X DELETE, `do git push`) are now caught by OUTWARD instead.
    elif irr >= 0.7 or (scope == "pattern" and irr >= 0.6) or (act["choice"] == "confirm" and act["confidence"] >= 0.8 and irr >= 0.6):
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
