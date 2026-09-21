#!/usr/bin/env bash
# End-to-end exercise of every hook against the stub Jev. No key, no network.
set -u
cd "$(dirname "$0")/.."
PORT=${PORT:-$(python3 -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1])')}
# Every raw request, so a test can assert what a hook does NOT send. Must be
# exported before the stub starts: the stub reads it from its own environment.
export JEV_STUB_RECORD="$(mktemp)"
python3 test/stub_jev.py "$PORT" & STUB=$!
trap 'kill $STUB 2>/dev/null' EXIT
sleep 0.4
export TYPESAFE_BASE_URL="http://127.0.0.1:$PORT" TYPESAFE_API_KEY=stub-key
export CLAUDE_PLUGIN_DATA="$(mktemp -d)"
pass=0; fail=0
check() { if eval "$2"; then echo "PASS  $1"; pass=$((pass+1)); else echo "FAIL  $1"; fail=$((fail+1)); fi; }

# --- bash risk gate
out=$(printf '%s' '{"tool_name":"Bash","tool_input":{"command":"git status && go test ./..."}}' | python3 hooks/bash_risk_gate.py)
check "gate: read-only command never reaches Jev" '[ -z "$out" ]'
out=$(printf '%s' '{"tool_name":"Bash","tool_input":{"command":"pkill -f db.test # [self_kill=yes]"}}' | python3 hooks/bash_risk_gate.py)
check "gate: self-kill pattern is denied" 'echo "$out" | grep -q "\"permissionDecision\": \"deny\""'
out=$(printf '%s' '{"tool_name":"Bash","tool_input":{"command":"rm -rf build/* # [irreversible=yes] [scope=pick:pattern] [action=pick:confirm]"}}' | python3 hooks/bash_risk_gate.py)
check "gate: irreversible pattern asks" 'echo "$out" | grep -q "\"permissionDecision\": \"ask\""'
out=$(printf '%s' '{"tool_name":"Bash","tool_input":{"command":"pkill -f db.test # [self_kill=yes]"}}' | JEV_HOOKS_GATE_MODE=warn python3 hooks/bash_risk_gate.py)
check "gate: warn mode downgrades deny to ask" 'echo "$out" | grep -q "\"permissionDecision\": \"ask\""'
out=$(printf '%s' '{"tool_name":"Bash","tool_input":{"command":"rm -rf /tmp/x"}}' | TYPESAFE_BASE_URL=http://127.0.0.1:1 python3 hooks/bash_risk_gate.py)
check "gate: Jev unreachable fails open" '[ -z "$out" ]'

# A prompt that fires on a third of all commands is a prompt nobody reads
# (2026-09-21: over 1042 logged gate decisions, a bare `confirm` verdict drove
# 256 of 259 prompts, mostly heredoc edits and scratchpad writes, while 88 were
# actually irreversible). A confirm now needs confidence AND something at stake.
out=$(printf '%s' '{"tool_name":"Bash","tool_input":{"command":"python3 - <<PY # [irreversible=no] [action=pick:confirm]\nopen(\"test/run.sh\").read()\nPY"}}' | python3 hooks/bash_risk_gate.py)
check "gate: a confirm with nothing at stake no longer interrupts" '[ -z "$out" ]'

# Outward-facing commands ask without a judgment call: nothing local is
# destroyed, so they score low, but no local undo reaches a merged PR either.
before=$(wc -l < "$JEV_STUB_RECORD")
out=$(printf '%s' '{"tool_name":"Bash","tool_input":{"command":"cd ~/apps/x && gh pr merge 13 --squash --delete-branch"}}' | python3 hooks/bash_risk_gate.py)
check "gate: merging a PR always asks" 'echo "$out" | grep -q "\"permissionDecision\": \"ask\"" && echo "$out" | grep -q "publishes, merges or deploys"'
check "gate: an outward command asks without spending a Jev call" '[ "$(wc -l < "$JEV_STUB_RECORD")" = "$before" ]'
out=$(printf '%s' '{"tool_name":"Bash","tool_input":{"command":"git push --dry-run origin main"}}' | python3 hooks/bash_risk_gate.py)
check "gate: a dry run is not a push" '! echo "$out" | grep -q "publishes, merges or deploys"'
# Anchored to a segment start, so prose that names a push is not one.
out=$(printf '%s' '{"tool_name":"Bash","tool_input":{"command":"echo \"then run git push origin main\" > /tmp/notes.txt"}}' | python3 hooks/bash_risk_gate.py)
check "gate: a command that merely MENTIONS a push is not treated as one" '! echo "$out" | grep -q "publishes, merges or deploys"'

# --- precompact triage + sessionstart reinject
printf '%s' "{\"session_id\":\"s1\",\"transcript_path\":\"$PWD/test/fixtures/session.jsonl\",\"hook_event_name\":\"PreCompact\",\"trigger\":\"auto\"}" | python3 hooks/precompact_triage.py
keep="$CLAUDE_PLUGIN_DATA/keep/s1.md"
check "triage: keep file written" '[ -s "$keep" ]'
check "triage: binding user rule kept" 'grep -q "55503" "$keep"'
check "triage: open question kept" 'grep -q "own Secret or the api" "$keep"'
check "triage: progress narration dropped" '! grep -q "will report back" "$keep"'
check "triage: superseded decision dropped" '! grep -q "fixed in #32" "$keep"'
out=$(printf '%s' '{"session_id":"s1","hook_event_name":"SessionStart","source":"compact"}' | python3 hooks/sessionstart_reinject.py)
check "reinject: prints the keep set after compaction" 'echo "$out" | grep -q "Preserved across compaction"'
out=$(printf '%s' '{"session_id":"nope","hook_event_name":"SessionStart","source":"compact"}' | python3 hooks/sessionstart_reinject.py)
check "reinject: silent when nothing was kept" '[ -z "$out" ]'

# --- subagent verify
msg="Done. PR https://example/pr/1. Wrote docs/CONTRACT.md with all shapes. [c0=yes] [c1=no] [c2=yes] [evidence=yes] [honest=no]"
in=$(python3 -c "import json,sys;print(json.dumps({'transcript_path':'$PWD/test/fixtures/subagent.jsonl','last_assistant_message':sys.argv[1],'stop_hook_active':False}))" "$msg")
out=$(printf '%s' "$in" | python3 hooks/subagent_verify.py)
check "verify: unmet criterion blocks the stop" 'echo "$out" | grep -q "\"decision\": \"block\""'
check "verify: reason names the unmet criterion" 'echo "$out" | grep -q "GRAPH.md gains a Judgment section"'
in=$(python3 -c "import json,sys;print(json.dumps({'transcript_path':'$PWD/test/fixtures/subagent.jsonl','last_assistant_message':sys.argv[1],'stop_hook_active':True}))" "$msg")
out=$(printf '%s' "$in" | python3 hooks/subagent_verify.py)
check "verify: never blocks twice (stop_hook_active)" '[ -z "$out" ]'
msg2="Done. [c0=yes] [c1=yes] [c2=yes] [evidence=yes]"
in=$(python3 -c "import json,sys;print(json.dumps({'transcript_path':'$PWD/test/fixtures/subagent.jsonl','last_assistant_message':sys.argv[1],'stop_hook_active':False}))" "$msg2")
out=$(printf '%s' "$in" | python3 hooks/subagent_verify.py)
check "verify: all criteria met passes silently" '[ -z "$out" ]'

# --- loop detect (hooks/loop_detect.py) ------------------------------------------------
ld() { printf '%s' "$1" | python3 hooks/loop_detect.py; }
ev() { python3 -c 'import json,sys;print(json.dumps({"session_id":sys.argv[1],"tool_name":sys.argv[2],"tool_input":{"command":sys.argv[3]},"tool_response":{"stdout":sys.argv[4],"exit_code":int(sys.argv[5])}}))' "$@"; }
rm -rf "$CLAUDE_PLUGIN_DATA/loops"
out=$(ld "$(ev ld1 Bash 'go build ./...' 'ok' 0)"); out2=$(ld "$(ev ld1 Read 'x.go' 'contents' 0)")
check "loop: no-repeat fast path is silent" '[ -z "$out" ] && [ -z "$out2" ]'
# Timing: take the BEST of three and allow for interpreter startup. An absolute
# wall-clock bound over a single run failed under machine load (measured 163-189
# ms against a 150 ms bound) — a check that fails randomly is one people learn
# to ignore. What actually matters is that the fast path costs no network call,
# which the call-count check below asserts; this only guards against the fast
# path accidentally growing a Jev call, which would cost hundreds of ms.
best=99999
for _ in 1 2 3; do
  t0=$(date +%s%N); ld "$(ev ld1 Bash 'go build ./...' 'ok' 0)" >/dev/null; t1=$(date +%s%N)
  one=$(( (t1 - t0) / 1000000 )); [ "$one" -lt "$best" ] && best=$one
done
check "loop: fast path stays local, no judgment call (best of 3: ${best} ms)" '[ "$best" -lt 400 ]'
check "loop: state keeps at most 6 pairs" 'for i in 1 2 3 4 5 6 7; do ld "$(ev ld1 Bash "cmd$i" out 0)" >/dev/null; done; python3 -c "import json,sys;d=json.load(open(sys.argv[1]));sys.exit(0 if len(d[\"pairs\"])==6 else 1)" "$CLAUDE_PLUGIN_DATA/loops/ld1.json"'
rm -rf "$CLAUDE_PLUGIN_DATA/loops"
# Three Bash calls in a row is a session, not a loop. Judging them cost 496
# calls on the hot path of every tool use and never once struck (2026-09-21).
before=$(wc -l < "$JEV_STUB_RECORD")
ld "$(ev ld5 Bash 'go build ./...' 'ok' 0)" >/dev/null
ld "$(ev ld5 Bash 'go vet ./...' 'ok' 0)" >/dev/null
ld "$(ev ld5 Bash 'gofmt -l .' '' 0)" >/dev/null
check "loop: three different successful calls of one tool never reach the judge" '[ "$(wc -l < "$JEV_STUB_RECORD")" = "$before" ]'
# The two marks worth judging: something failed, or the same call was repeated.
before=$(wc -l < "$JEV_STUB_RECORD")
for _ in 1 2 3; do ld "$(ev ld6 Bash 'go test ./pkg # [progress=level:1]' 'ok' 0)" >/dev/null; done
check "loop: the SAME call repeated is judged even when it succeeds" '[ "$(wc -l < "$JEV_STUB_RECORD")" -gt "$before" ]'
before=$(wc -l < "$JEV_STUB_RECORD")
ld "$(ev ld7 Bash 'go build ./a # [progress=level:1]' 'ok' 0)" >/dev/null
ld "$(ev ld7 Bash 'go build ./b # [progress=level:1]' 'ok' 0)" >/dev/null
ld "$(ev ld7 Bash 'go build ./c # [progress=level:1]' 'undefined: Foo' 1)" >/dev/null
check "loop: a failure among three same-tool calls is judged" '[ "$(wc -l < "$JEV_STUB_RECORD")" -gt "$before" ]'
rm -rf "$CLAUDE_PLUGIN_DATA/loops"
# A grep through source code is not a failing build: substring matching on
# "error" put 88% of all tool calls into the repeat check.
lk() { python3 -c "import sys,importlib.util;sys.path.insert(0,'lib');spec=importlib.util.spec_from_file_location('ld','hooks/loop_detect.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);print(m.looks_like_error(None,sys.argv[1]))" "$1"; }
check "loop: source code that mentions an error is not an error" '[ "$(lk "42:	if err != nil { return fmt.Errorf(\"parse error: %w\", err) }")" = "False" ]'
check "loop: a failure at the start of a line is an error" '[ "$(lk "--- FAIL: TestX (0.01s)")" = "True" ] && [ "$(lk "fatal: not a git repository")" = "True" ]'
o1=$(ld "$(ev ld2 Bash 'go test ./... # [progress=level:0]' 'FAIL TestX: boom' 1)")
o2=$(ld "$(ev ld2 Bash 'go test ./... # [progress=level:0]' 'FAIL TestX: boom' 1)")
o3=$(ld "$(ev ld2 Bash 'go test ./... # [progress=level:0]' 'FAIL TestX: boom' 1)")
check "loop: first repeat judged, one strike, no message" '[ -z "$o3" ] && python3 -c "import json,sys;d=json.load(open(sys.argv[1]));sys.exit(0 if d[\"strikes\"]==1 else 1)" "$CLAUDE_PLUGIN_DATA/loops/ld2.json"'
o4=$(ld "$(ev ld2 Bash 'go test ./... # [progress=level:0]' 'FAIL TestX: boom' 1)")
o5=$(ld "$(ev ld2 Bash 'go test ./... # [progress=level:0]' 'FAIL TestX: boom' 1)")
check "loop: third strike emits systemMessage naming tool and error" 'echo "$o5" | grep -q "systemMessage" && echo "$o5" | grep -q "three Bash calls" && echo "$o5" | grep -q "boom"'
check "loop: third strike adds additionalContext, never a decision" 'echo "$o5" | grep -q "additionalContext" && ! echo "$o5" | grep -q "\"decision\""'
check "loop: strikes reset after firing" 'python3 -c "import json,sys;d=json.load(open(sys.argv[1]));sys.exit(0 if d[\"strikes\"]==0 else 1)" "$CLAUDE_PLUGIN_DATA/loops/ld2.json"'
# Seed two strikes with unmarked earlier attempts (the stub reads the FIRST marker in the request,
# and the whole three-call window is sent), then a level-2 attempt must reset to zero.
python3 -c 'import json,sys;json.dump({"pairs":[{"tool":"Bash","input":"go test ./...","result":"FAIL","error":True}]*2,"strikes":2},open(sys.argv[1],"w"))' "$CLAUDE_PLUGIN_DATA/loops/ld4.json"
o6=$(ld "$(ev ld4 Bash 'go test ./... # [progress=level:2]' 'ok' 0)")
check "loop: a level-2 repeat resets strikes" '[ -z "$o6" ] && python3 -c "import json,sys;d=json.load(open(sys.argv[1]));sys.exit(0 if d[\"strikes\"]==0 else 1)" "$CLAUDE_PLUGIN_DATA/loops/ld4.json"'
o7=$(TYPESAFE_BASE_URL=http://127.0.0.1:1 ld "$(ev ld3 Bash 'x' 'FAIL' 1)"; TYPESAFE_BASE_URL=http://127.0.0.1:1 ld "$(ev ld3 Bash 'x' 'FAIL' 1)"; TYPESAFE_BASE_URL=http://127.0.0.1:1 ld "$(ev ld3 Bash 'x' 'FAIL' 1)")
check "loop: judge unreachable fails open" '[ -z "$o7" ]'
# --- stop self-check (hooks/stop_selfcheck.py)
sf="$PWD/test/fixtures/stop_session.jsonl"
stopin() { python3 -c "import json,sys;print(json.dumps({'transcript_path':sys.argv[1],'last_assistant_message':sys.argv[2],'stop_hook_active':sys.argv[3]=='1','session_id':'s'}))" "$sf" "$1" "$2"; }
out=$(stopin "Build is green. I'll open the PR next. [promise=yes] [unanswered=no] [unverified=no]" 0 | python3 hooks/stop_selfcheck.py)
check "stop: a promise of undone work blocks" 'echo "$out" | grep -q "\"decision\": \"block\"" && echo "$out" | grep -q "promises work not yet done"'
check "stop: reason quotes the offending sentence" 'echo "$out" | grep -q "open the PR next"'
out=$(stopin "The build compiles. [promise=no] [unanswered=yes] [unverified=no]" 0 | python3 hooks/stop_selfcheck.py)
check "stop: an unanswered question blocks" 'echo "$out" | grep -q "does not answer what the user last asked"'
out=$(stopin "All 212 integration tests pass. [promise=no] [unanswered=no] [unverified=yes]" 0 | python3 hooks/stop_selfcheck.py)
check "stop: an unverified outcome blocks" 'echo "$out" | grep -q "tool results do not show"'
out=$(stopin "Build is green; the integration suite was not run, only go build. [promise=no] [unanswered=no] [unverified=no]" 0 | python3 hooks/stop_selfcheck.py)
check "stop: honest complete message passes silently" '[ -z "$out" ]'
out=$(stopin "I'll do it next. [promise=yes] [unanswered=yes] [unverified=yes]" 1 | python3 hooks/stop_selfcheck.py)
check "stop: never blocks twice (stop_hook_active)" '[ -z "$out" ]'
out=$(stopin "I'll do it next. [promise=yes]" 0 | TYPESAFE_BASE_URL=http://127.0.0.1:1 python3 hooks/stop_selfcheck.py)
check "stop: Jev unreachable fails open" '[ -z "$out" ]'
# --- end stop self-check

# --- audit fixes (2026-09-19) ---------------------------------------------------------
gate_fast() { printf '%s' "$(python3 -c 'import json,sys;print(json.dumps({"tool_name":"Bash","tool_input":{"command":sys.argv[1]}}))' "$1")" | JEV_HOOKS_DEBUG=1 TYPESAFE_BASE_URL=http://127.0.0.1:1 python3 hooks/bash_risk_gate.py 2>&1 >/dev/null | grep -c "fast-path"; }
bypass_ok=1
while IFS= read -r c; do [ -z "$c" ] && continue; if [ "$(gate_fast "$c")" != "0" ]; then echo "      still fast-path: $c"; bypass_ok=0; fi; done <<'EOF'
echo x > /etc/passwd
cat a > b
find . -delete
find . -exec rm -rf {} \;
env X=1 rm -rf /tmp/x
echo $(rm -rf x)
echo `rm -rf x`
awk 'BEGIN{system("rm -rf x")}'
git branch -D master
git remote remove origin
sort -o /etc/hosts
go test -exec 'rm -rf /' ./...
ls | xargs rm
cat a | tee /etc/hosts
EOF
printf 'ls\nrm -rf /tmp/x' > "$CLAUDE_PLUGIN_DATA/nl.txt"; [ "$(gate_fast "$(cat "$CLAUDE_PLUGIN_DATA/nl.txt")")" = "0" ] || { echo "      still fast-path: newline-joined rm"; bypass_ok=0; }
check "gate: none of the audit's bypasses takes the fast path" '[ "$bypass_ok" = "1" ]'
ro_ok=1
while IFS= read -r c; do [ -z "$c" ] && continue; if [ "$(gate_fast "$c")" != "1" ]; then echo "      lost fast-path: $c"; ro_ok=0; fi; done <<'EOF'
git status && go test ./...
ls -la | head -5
grep -rn foo . 2>&1 | wc -l
go build ./... >/dev/null 2>&1
git branch
git remote -v
kubectl get pods
EOF
check "gate: plain read-only commands still take the fast path" '[ "$ro_ok" = "1" ]'

# verify: criteria extraction picks deliverables, not context
crit_ok=$(python3 - <<'PYEOF'
import sys, importlib.util
sys.path.insert(0, "lib")
spec = importlib.util.spec_from_file_location("sv", "hooks/subagent_verify.py"); sv = importlib.util.module_from_spec(spec); spec.loader.exec_module(sv)
ctx = "\n".join(f"- context note number {i} about the repository layout and history" for i in range(20))
dl = "\n".join(f"{i}. deliverable number {i}: produce artifact {i} with a test that proves it" for i in range(1, 21))
p = "Background:\n" + ctx + "\n\nDeliverables:\n" + dl + "\n- Option A: do it in Go instead of Python if you prefer\n- Is this the right approach?\n"
c = sv.criteria_from(p)
ok = len(c) == 14 and all(x.startswith("deliverable number") for x in c) and c[-1].startswith("deliverable number 20") and not any("Option" in x or x.endswith("?") for x in c)
print("1" if ok else "0")
PYEOF
)
check "verify: 20 context bullets then 20 numbered deliverables -> the last 14 deliverables, no options or questions" '[ "$crit_ok" = "1" ]'

# transcript: a prompt wrapped in a system-reminder is not dropped
printf '%s\n' '{"type":"user","message":{"role":"user","content":"<system-reminder>\nhouse rules here\n</system-reminder>\nExecute node Q.\n1. write the thing and prove it works with a test"}}' '{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"Done."}]}}' > "$CLAUDE_PLUGIN_DATA/wrapped.jsonl"
tp=$(python3 -c "import sys;sys.path.insert(0,'lib');import transcript;print(transcript.task_prompt(sys.argv[1])[:15])" "$CLAUDE_PLUGIN_DATA/wrapped.jsonl")
check "transcript: system-reminder wrapper is stripped, the task survives" '[ "$tp" = "Execute node Q." ]'

# loop: 12 parallel invocations keep a well-formed window
rm -f "$CLAUDE_PLUGIN_DATA/loops/par.json" "$CLAUDE_PLUGIN_DATA/loops/par.json.lock"
pids=""; for i in $(seq 0 11); do ld "$(ev par T$i "cmd$i" out 0)" >/dev/null & pids="$pids $!"; done; wait $pids
check "loop: 12 parallel calls leave a well-formed 6-entry window" 'python3 test/check_window.py "$CLAUDE_PLUGIN_DATA/loops/par.json"'

# triage: bounded work on a huge transcript
python3 test/gen_huge.py "$CLAUDE_PLUGIN_DATA/huge.jsonl"
export JEV_HOOKS_LOG="$CLAUDE_PLUGIN_DATA/triage.log"; rm -f "$JEV_HOOKS_LOG"
t0=$(date +%s); printf '%s' "{\"session_id\":\"huge\",\"transcript_path\":\"$CLAUDE_PLUGIN_DATA/huge.jsonl\",\"hook_event_name\":\"PreCompact\"}" | python3 hooks/precompact_triage.py; t1=$(date +%s)
calls=$(wc -l < "$JEV_HOOKS_LOG"); unset JEV_HOOKS_LOG
check "triage: a 2000-segment transcript costs at most 20 calls (made $calls) in under 30s ($((t1-t0))s)" '[ "$calls" -le 20 ] && [ $((t1-t0)) -lt 30 ]'

# jev: a 429 with Retry-After is retried and succeeds
ra=$(python3 -c "
import sys; sys.path.insert(0,'lib'); import jev
a=jev.ask({'x':'[stub=429once:tok1] [q=yes]'}, {'q': jev.noul('is it?')})
print('1' if a and a['q']['noul']==1.0 else '0')")
check "jev: a 429 with Retry-After is retried once and succeeds" '[ "$ra" = "1" ]'

# session_id sanitised for file paths
printf '%s' "{\"session_id\":\"../../evil\",\"transcript_path\":\"$PWD/test/fixtures/session.jsonl\",\"hook_event_name\":\"PreCompact\"}" | python3 hooks/precompact_triage.py
check "paths: a traversal session_id stays inside the keep dir" '[ -f "$CLAUDE_PLUGIN_DATA/keep/evil.md" ] && [ ! -e "$CLAUDE_PLUGIN_DATA/../evil.md" ] && [ ! -e "$CLAUDE_PLUGIN_DATA/../../evil.md" ]'

# prune: old files are removed
touch -d "20 days ago" "$CLAUDE_PLUGIN_DATA/keep/old.md" "$CLAUDE_PLUGIN_DATA/loops/old.json"
ld "$(ev pr1 Bash x out 0)" >/dev/null; printf '%s' "{\"session_id\":\"pr2\",\"transcript_path\":\"$PWD/test/fixtures/session.jsonl\"}" | python3 hooks/precompact_triage.py
check "prune: keep and loop files older than 14 days are deleted" '[ ! -e "$CLAUDE_PLUGIN_DATA/keep/old.md" ] && [ ! -e "$CLAUDE_PLUGIN_DATA/loops/old.json" ]'
# --- end audit fixes

# --- prompt routing (UserPromptSubmit) ---------------------------------------
pr() { printf '%s' "$1" | JEV_HOOKS_DEBUG=1 python3 hooks/prompt_routing.py 2>"$CLAUDE_PLUGIN_DATA/pr.err"; }
out=$(pr '{"prompt":"why does the frontier release cancelled dependents? [kind=pick:question] [skill=pick:none]","cwd":"/tmp"}')
check "routing: a question gets the answer-and-report hint" 'echo "$out" | grep -q "reads as a question"'
out=$(pr '{"prompt":"yes merge 58 and go please [kind=pick:approval] [skill=pick:none]","cwd":"/tmp"}')
check "routing: an approval gets the proceed hint" 'echo "$out" | grep -q "reads as approval"'
out=$(pr '{"prompt":"i keep thinking the projection is the wrong shape [kind=pick:thinking_aloud] [skill=pick:none]","cwd":"/tmp"}')
check "routing: thinking aloud gets the do-not-start hint" 'echo "$out" | grep -q "reads as thinking aloud"'
out=$(pr '{"prompt":"add an index on runs.started_at please [kind=pick:change_request] [skill=pick:none]","cwd":"/tmp"}')
check "routing: a change request prints nothing" '[ -z "$out" ]'
out=$(pr '{"prompt":"ok go","cwd":"/tmp"}')
check "routing: a prompt under 12 chars skips the call" '[ -z "$out" ] && grep -q "^skip" "$CLAUDE_PLUGIN_DATA/pr.err"'
out=$(pr '{"prompt":"/compact please do it right now","cwd":"/tmp"}')
check "routing: a slash command skips the call" '[ -z "$out" ] && grep -q "^skip" "$CLAUDE_PLUGIN_DATA/pr.err"'
out=$(pr '{"prompt":"<bash-input>ls -la</bash-input><bash-stdout>x y z</bash-stdout>","cwd":"/tmp"}')
check "routing: a fully wrapped prompt skips the call" '[ -z "$out" ] && grep -q "^skip" "$CLAUDE_PLUGIN_DATA/pr.err"'
# skill pick: a project skills dir with two names; the stub picks the steered one
SK="$CLAUDE_PLUGIN_DATA/proj"; mkdir -p "$SK/.claude/skills/deploy-thing" "$SK/.claude/skills/write-docs" "$SK/sub/dir"
out=$(pr "{\"prompt\":\"deploy the thing to staging now [kind=pick:change_request] [skill=pick:deploy-thing]\",\"cwd\":\"$SK/sub/dir\"}")
check "routing: a project skill is found walking up from cwd and named in the hint" 'echo "$out" | grep -q "Relevant skill: deploy-thing\."'
out=$(pr "{\"prompt\":\"why does the frontier release cancelled dependents? [kind=pick:question] [skill=pick:write-docs]\",\"cwd\":\"$SK\"}")
check "routing: kind hint and skill hint combine on one line" '[ "$(echo "$out" | wc -l)" -eq 1 ] && echo "$out" | grep -q "reads as a question" && echo "$out" | grep -q "Relevant skill: write-docs\."'
# more than 100 skills: no skill question at all, so a steered pick cannot appear
MANY="$CLAUDE_PLUGIN_DATA/many"; for i in $(seq 1 101); do mkdir -p "$MANY/.claude/skills/s$i"; done
out=$(pr "{\"prompt\":\"deploy the thing to staging now [kind=pick:change_request] [skill=pick:s7]\",\"cwd\":\"$MANY\"}")
check "routing: more than 100 skill names skips the skill question" '[ -z "$out" ]'
out=$(printf '%s' '{"prompt":"why is the build red this morning then? [kind=pick:question]","cwd":"/tmp"}' | TYPESAFE_BASE_URL=http://127.0.0.1:1 python3 hooks/prompt_routing.py)
check "routing: Jev unreachable is silent" '[ -z "$out" ]'
s=$(date +%s%N); pr '{"prompt":"why does the frontier release cancelled dependents? [kind=pick:question] [skill=pick:none]","cwd":"/tmp"}' >/dev/null; e=$(date +%s%N)
check "routing: under 400 ms against the stub ($(( (e-s)/1000000 )) ms)" '[ $(( (e-s)/1000000 )) -lt 400 ]'
# --- end prompt routing

# --- task_prompt: a transcript whose first prompt is a compaction summary (verifier bug 2026-09-19)
tp=$(python3 -c "import sys;sys.path.insert(0,'lib');import transcript;print(transcript.task_prompt('test/fixtures/summary_first.jsonl').splitlines()[0])")
check "task_prompt: compaction summary first -> the last Agent call is the task" '[ "$tp" = "REAL TASK" ]'
tp2=$(python3 -c "import sys;sys.path.insert(0,'lib');import transcript;print(transcript.task_prompt('test/fixtures/subagent.jsonl')[:14])")
check "task_prompt: plain agent transcript still uses its first prompt" '[ "$tp2" = "Execute node X" ]'

# --- stop check: a next step blocked on the user is not a promise (2026-09-20)
gate_msg="The release is merged and both images are built; migrations 020 and 021 applied. The roll PR is #402 and needs your merge: gh pr merge 402. [promise=no] [unanswered=no] [unverified=no]"
gin=$(python3 -c "import json,sys;print(json.dumps({'transcript_path':'$PWD/test/fixtures/gate_session.jsonl','last_assistant_message':sys.argv[1],'stop_hook_active':False}))" "$gate_msg")
gout=$(printf '%s' "$gin" | python3 hooks/stop_selfcheck.py)
check "stop check: a step blocked on the user, after a report, passes" '[ -z "$gout" ]'
promise_msg="I'll open the PR next and report back. [promise=yes] [unanswered=no] [unverified=no]"
pin=$(python3 -c "import json,sys;print(json.dumps({'transcript_path':'$PWD/test/fixtures/gate_session.jsonl','last_assistant_message':sys.argv[1],'stop_hook_active':False}))" "$promise_msg")
pout=$(printf '%s' "$pin" | python3 hooks/stop_selfcheck.py)
check "stop check: a plain promise still blocks" 'echo "$pout" | grep -q "\"decision\": \"block\""'

# --- stop check: an expected result from a check not yet run is not an unverified claim
fut_msg="The fix is merged and the image is built. The roll will show 200 instead of 404 once applied. [promise=no] [unanswered=no] [unverified=no]"
fin=$(python3 -c "import json,sys;print(json.dumps({'transcript_path':'$PWD/test/fixtures/gate_session.jsonl','last_assistant_message':sys.argv[1],'stop_hook_active':False}))" "$fut_msg")
fout=$(printf '%s' "$fin" | python3 hooks/stop_selfcheck.py)
check "stop check: an expected future result passes" '[ -z "$fout" ]'
past_msg="All 41 checks pass and the release is deployed. [promise=no] [unanswered=no] [unverified=yes]"
pin2=$(python3 -c "import json,sys;print(json.dumps({'transcript_path':'$PWD/test/fixtures/gate_session.jsonl','last_assistant_message':sys.argv[1],'stop_hook_active':False}))" "$past_msg")
pout2=$(printf '%s' "$pin2" | python3 hooks/stop_selfcheck.py)
check "stop check: an unevidenced PAST result still blocks" 'echo "$pout2" | grep -q "\"decision\": \"block\""'

# --- decision log: hooks record what they decided, and a dispute is labelled data
dl=$(mktemp); rm -f "$dl"
JEV_HOOKS_LOG="$dl" python3 -c "
import sys,os; sys.path.insert(0,'lib'); import jev
jev.record('stop_check','block',{'promise':{'noul':0.92}},note='promise')
jev.record('stop_check','pass',{'promise':{'noul':0.10}})
jev.record('bash_gate','ask',{'irreversible':{'noul':0.71},'action':{'choice':'confirm','confidence':0.83}},command='rm -rf build')
" >/dev/null 2>&1
dcount=$(python3 -c "
import json,sys
print(sum(1 for l in open(sys.argv[1]) if json.loads(l).get('kind')=='decision' and json.loads(l).get('probs')))
" "$dl")
check "decision log: three decisions recorded with their probabilities" '[ "$dcount" = "3" ]'
JEV_HOOKS_LOG="$dl" python3 bin/wrong.py stop_check "was blocked on the user" >/dev/null
dtarget=$(python3 -c "
import json,sys
print(next(json.loads(l)['of']['decision'] for l in open(sys.argv[1]) if json.loads(l).get('kind')=='dispute'))
" "$dl")
check "dispute: marks the last SPOKEN decision, not a silent pass" '[ "$dtarget" = "block" ]'
st=$(JEV_HOOKS_LOG="$dl" python3 bin/stats.py)
check "stats: reports the hooks, the spoke rate and the dispute" 'echo "$st" | grep -q "stop_check" && echo "$st" | grep -q "disputed 1"'
check "stats: names the near-threshold band that needs tuning" 'echo "$st" | grep -q "near a threshold"'
JEV_HOOKS_LOG=off python3 -c "
import sys; sys.path.insert(0,'lib'); import jev
assert jev.LOG is None, 'JEV_HOOKS_LOG=off must disable recording'
" 2>/dev/null
check "decision log: JEV_HOOKS_LOG=off records nothing" '[ $? -eq 0 ]'
rm -f "$dl"

# --- narrow output (PreToolUse: Bash|Read|Grep) -------------------------------
NWD="$CLAUDE_PLUGIN_DATA/narrow"; mkdir -p "$NWD"
BIG="$NWD/big.txt"
python3 -c "import sys; open(sys.argv[1],'w').write(''.join('line %d\n' % i for i in range(1,1001)))" "$BIG"
# Read's fast path skips anything under 64 KB, so the Read cases need a file over it:
# 1000 lines still, but padded, so the offset arithmetic stays checkable.
BIGR="$NWD/bigread.txt"
python3 -c "import sys; open(sys.argv[1],'w').write(''.join(('line %d ' % i).ljust(120) + '\n' for i in range(1,1001)))" "$BIGR"
SMALLR="$NWD/small.txt"
python3 -c "import sys; open(sys.argv[1],'w').write('x\n' * 50)" "$SMALLR"
# stdin for the hook: a one-prompt transcript carries the goal (and the stub's markers).
nw_in() { python3 - "$1" "$2" "$3" "$NWD" <<'PYEOF'
import json, os, sys
goal, tool, ti, d = sys.argv[1:5]
tp = os.path.join(d, "t.jsonl")
with open(tp, "w") as f:
    f.write(json.dumps({"type": "user", "message": {"role": "user", "content": goal}}) + "\n")
print(json.dumps({"tool_name": tool, "tool_input": json.loads(ti), "cwd": d,
                  "transcript_path": tp, "session_id": "nw"}))
PYEOF
}
nw_calls() { python3 -c "
import json, sys
try: ls = open(sys.argv[1]).read().splitlines()
except Exception: ls = []
print(sum(1 for l in ls if l.strip() and json.loads(l).get('kind') == 'call'))" "$1"; }
nw_field() { python3 -c "
import json, sys
try: o = json.loads(sys.stdin.read() or '{}')
except Exception: o = {}
cur = o
for k in sys.argv[1].split('.'):
    cur = (cur or {}).get(k) if isinstance(cur, dict) else None
print('' if cur is None else cur if isinstance(cur, str) else json.dumps(cur))" "$1"; }

nwlog="$NWD/calls.log"
# 1. a bulky read-only command is narrowed, and the rewrite really truncates
: > "$nwlog"
bulky_in=$(nw_in "why did the service restart [bulky=yes] [needs_all=no] [where=pick:head]" Bash "$(python3 -c 'import json,sys;print(json.dumps({"command":"cat "+sys.argv[1],"description":"read the log","timeout":120000}))' "$BIG")")
nwout=$(printf '%s' "$bulky_in" | JEV_HOOKS_LOG="$nwlog" python3 hooks/narrow_output.py)
newcmd=$(printf '%s' "$nwout" | nw_field hookSpecificOutput.updatedInput.command)
ran=$(bash -c "$newcmd" 2>&1)
check "narrow: a bulky read-only command is narrowed" '[ -n "$newcmd" ] && [ "$newcmd" != "cat $BIG" ]'
check "narrow: the rewritten command really truncates (200 lines + marker)" '[ "$(printf "%s\n" "$ran" | wc -l)" -eq 201 ] && printf "%s\n" "$ran" | grep -q "^line 200$" && ! printf "%s\n" "$ran" | grep -q "^line 201$"'
check "narrow: the marker names the TRUE total, which only counting the whole output can know" 'printf "%s\n" "$ran" | tail -1 | grep -q "jev-hooks: showing first 200 of 1000 lines"'
check "narrow: a systemMessage names what was narrowed and why" 'printf "%s" "$nwout" | grep -q "systemMessage" && printf "%s" "$nwout" | grep -q "bulky=1.00, needs_all=0.00"'
check "narrow: additionalContext says this is not the whole output" 'printf "%s" "$nwout" | nw_field hookSpecificOutput.additionalContext | grep -q "not the whole output"'
onlycmd=$(printf '%s' "$nwout" | python3 -c "
import json, sys
o = json.load(sys.stdin)['hookSpecificOutput']['updatedInput']
orig = json.loads(sys.argv[1])['tool_input']
diff = sorted(k for k in set(o) | set(orig) if o.get(k) != orig.get(k))
print('1' if diff == ['command'] and orig['command'] in o['command'] else '0')" "$bulky_in")
check "narrow: updatedInput for Bash changes only the command, and appends to it" '[ "$onlycmd" = "1" ]'

# 2. the tail form keeps the END of the output and says so
tailout=$(nw_in "what are the most recent lines [bulky=yes] [needs_all=no] [where=pick:tail]" Bash "$(python3 -c 'import json,sys;print(json.dumps({"command":"cat "+sys.argv[1]}))' "$BIG")" | python3 hooks/narrow_output.py)
tailran=$(bash -c "$(printf '%s' "$tailout" | nw_field hookSpecificOutput.updatedInput.command)" 2>&1)
check "narrow: where=tail keeps the last 200 lines and marks them" 'printf "%s\n" "$tailran" | grep -q "^line 1000$" && ! printf "%s\n" "$tailran" | grep -q "^line 800$" && printf "%s\n" "$tailran" | tail -1 | grep -q "showing last 200 of 1000 lines"'

# 3. a command whose trailing comment would swallow a `;` still rewrites correctly
cmtout=$(nw_in "why did it restart [bulky=yes] [needs_all=no] [where=pick:head]" Bash "$(python3 -c 'import json,sys;print(json.dumps({"command":"cat "+sys.argv[1]+"   # trailing comment"}))' "$BIG")" | python3 hooks/narrow_output.py)
cmtran=$(bash -c "$(printf '%s' "$cmtout" | nw_field hookSpecificOutput.updatedInput.command)" 2>&1)
check "narrow: a command ending in a comment still truncates (no swallowed brace)" '[ "$(printf "%s\n" "$cmtran" | wc -l)" -eq 201 ] && printf "%s\n" "$cmtran" | tail -1 | grep -q "jev-hooks:"'

# 4. needs_all: truncating would give a wrong answer, so leave it alone
: > "$nwlog"
naout=$(nw_in "how many rows are in it [bulky=yes] [needs_all=yes]" Bash "$(python3 -c 'import json,sys;print(json.dumps({"command":"cat "+sys.argv[1]}))' "$BIG")" | JEV_HOOKS_LOG="$nwlog" python3 hooks/narrow_output.py)
check "narrow: needs_all leaves the command untouched" '[ -z "$naout" ] && [ "$(nw_calls "$nwlog")" = "1" ]'

# 5. not provably read-only: never narrowed, and never judged
: > "$nwlog"
roout=$(nw_in "clean the build [bulky=yes] [needs_all=no]" Bash '{"command":"rm -rf build/"}' | JEV_HOOKS_DEBUG=1 JEV_HOOKS_LOG="$nwlog" python3 hooks/narrow_output.py 2>"$NWD/ro.err")
check "narrow: a non-read-only command is never narrowed and makes no call" '[ -z "$roout" ] && [ "$(nw_calls "$nwlog")" = "0" ] && grep -q "fast-path: not-read-only" "$NWD/ro.err"'
: > "$nwlog"
wrout=$(nw_in "save the log [bulky=yes] [needs_all=no]" Bash '{"command":"cat /var/log/syslog > /tmp/out.txt"}' | JEV_HOOKS_LOG="$nwlog" python3 hooks/narrow_output.py)
check "narrow: a redirect is not read-only, so it is left alone" '[ -z "$wrout" ] && [ "$(nw_calls "$nwlog")" = "0" ]'

# 6. already limited: no call to pay for
nwlimit_ok=1
while IFS= read -r c; do
  [ -z "$c" ] && continue
  : > "$nwlog"
  o=$(nw_in "why did it restart [bulky=yes] [needs_all=no]" Bash "$(python3 -c 'import json,sys;print(json.dumps({"command":sys.argv[1]}))' "$c")" | JEV_HOOKS_LOG="$nwlog" python3 hooks/narrow_output.py)
  [ -z "$o" ] && [ "$(nw_calls "$nwlog")" = "0" ] || { echo "      still judged: $c"; nwlimit_ok=0; }
done <<'EOF'
tail -100 /var/log/syslog
cat /var/log/syslog | head -50
grep -c TODO .
wc -l big.csv
git log --oneline -20
grep -rn --max-count=3 TODO .
EOF
check "narrow: an already-limited command makes no call" '[ "$nwlimit_ok" = "1" ]'

# 7. the kill switch, and failing open
: > "$nwlog"
offout=$(nw_in "why did the service restart [bulky=yes] [needs_all=no]" Bash "$(python3 -c 'import json,sys;print(json.dumps({"command":"cat "+sys.argv[1]}))' "$BIG")" | JEV_HOOKS_NARROW=off JEV_HOOKS_LOG="$nwlog" python3 hooks/narrow_output.py)
check "narrow: JEV_HOOKS_NARROW=off disables the hook, no call" '[ -z "$offout" ] && [ "$(nw_calls "$nwlog")" = "0" ]'
unreach=$(nw_in "why did the service restart [bulky=yes] [needs_all=no]" Bash "$(python3 -c 'import json,sys;print(json.dumps({"command":"cat "+sys.argv[1]}))' "$BIG")" | TYPESAFE_BASE_URL=http://127.0.0.1:1 python3 hooks/narrow_output.py)
check "narrow: Jev unreachable leaves the command untouched" '[ -z "$unreach" ]'
: > "$nwlog"
noglout=$(printf '%s' "$(python3 -c 'import json,sys;print(json.dumps({"tool_name":"Bash","tool_input":{"command":"cat "+sys.argv[1]},"transcript_path":"/nope/nothing.jsonl"}))' "$BIG")" | JEV_HOOKS_LOG="$nwlog" python3 hooks/narrow_output.py)
check "narrow: no goal in the transcript means no judgment and no call" '[ -z "$noglout" ] && [ "$(nw_calls "$nwlog")" = "0" ]'
check "narrow: malformed stdin is silent" '[ -z "$(printf "not json" | python3 hooks/narrow_output.py)" ]'

# 8. Read gets a limit, never a shell pipe
rdout=$(nw_in "what does this config set [bulky=yes] [needs_all=no] [where=pick:head]" Read "$(python3 -c 'import json,sys;print(json.dumps({"file_path":sys.argv[1]}))' "$BIGR")" | python3 hooks/narrow_output.py)
rdin=$(printf '%s' "$rdout" | nw_field hookSpecificOutput.updatedInput)
check "narrow: a Read gets limit=200 and keeps its file_path" 'echo "$rdin" | python3 -c "
import json,sys
d=json.load(sys.stdin); sys.exit(0 if d.get(\"limit\")==200 and d.get(\"file_path\")==sys.argv[1] else 1)" "$BIGR"'
check "narrow: a Read is never rewritten into a shell pipe" '! echo "$rdin" | grep -q "command" && ! echo "$rdin" | grep -q "head -"'
check "narrow: a Read with where=head sets no offset" '! echo "$rdin" | grep -q "offset"'
rdtail=$(nw_in "what does the end of it say [bulky=yes] [needs_all=no] [where=pick:tail]" Read "$(python3 -c 'import json,sys;print(json.dumps({"file_path":sys.argv[1]}))' "$BIGR")" | python3 hooks/narrow_output.py | nw_field hookSpecificOutput.updatedInput)
check "narrow: a Read with where=tail offsets to the last 200 lines of 1000" 'echo "$rdtail" | python3 -c "
import json,sys
d=json.load(sys.stdin); sys.exit(0 if d.get(\"offset\")==801 and d.get(\"limit\")==200 else 1)"'
: > "$nwlog"
rdlim=$(nw_in "what does this config set [bulky=yes] [needs_all=no]" Read '{"file_path":"/etc/hosts","limit":40}' | JEV_HOOKS_LOG="$nwlog" python3 hooks/narrow_output.py)
check "narrow: a Read that already has a limit makes no call" '[ -z "$rdlim" ] && [ "$(nw_calls "$nwlog")" = "0" ]'

# 9. Grep gets head_limit, and a count is already an answer
grout=$(nw_in "where is the retry handled [bulky=yes] [needs_all=no]" Grep '{"pattern":"TODO","path":".","output_mode":"content"}' | python3 hooks/narrow_output.py | nw_field hookSpecificOutput.updatedInput)
check "narrow: a Grep gets head_limit=100 and keeps its pattern and path" 'echo "$grout" | python3 -c "
import json,sys
d=json.load(sys.stdin); sys.exit(0 if d.get(\"head_limit\")==100 and d.get(\"pattern\")==\"TODO\" and d.get(\"path\")==\".\" else 1)"'
: > "$nwlog"
grcount=$(nw_in "how many TODOs are there [bulky=yes] [needs_all=no]" Grep '{"pattern":"TODO","path":".","output_mode":"count"}' | JEV_HOOKS_LOG="$nwlog" python3 hooks/narrow_output.py)
check "narrow: a Grep in count mode is already an answer, no call" '[ -z "$grcount" ] && [ "$(nw_calls "$nwlog")" = "0" ]'

# 10. the decision is on the record either way
: > "$nwlog"
nw_in "why did the service restart [bulky=yes] [needs_all=no]" Bash "$(python3 -c 'import json,sys;print(json.dumps({"command":"cat "+sys.argv[1]}))' "$BIG")" | JEV_HOOKS_LOG="$nwlog" python3 hooks/narrow_output.py >/dev/null
nw_in "how many rows [bulky=yes] [needs_all=yes]" Bash "$(python3 -c 'import json,sys;print(json.dumps({"command":"cat "+sys.argv[1]}))' "$BIG")" | JEV_HOOKS_LOG="$nwlog" python3 hooks/narrow_output.py >/dev/null
nwrec=$(python3 -c "
import json, sys
ds = [json.loads(l) for l in open(sys.argv[1]) if l.strip() and json.loads(l).get('kind') == 'decision']
print(','.join(d['decision'] for d in ds if d.get('hook') == 'narrow' and d.get('probs')))" "$nwlog")
check "narrow: both the narrowed and the silent decision are recorded with probabilities" '[ "$nwrec" = "narrowed,silent" ]'

# 11. the rewrite reports the command's REAL exit status, and never eats stderr
failcmd=$(nw_in "why did the service restart [bulky=yes] [needs_all=no] [where=pick:head]" Bash '{"command":"cat /nonexistent-path-xyz"}' | python3 hooks/narrow_output.py | nw_field hookSpecificOutput.updatedInput.command)
failout=$(bash -c "$failcmd" 2>"$NWD/fail.err"); failrc=$?
check "narrow: a failing read-only command keeps its non-zero exit status through the rewrite" '[ -n "$failcmd" ] && [ "$failrc" -ne 0 ]'
check "narrow: stderr is never buffered or narrowed, so the error still reaches the caller" 'grep -qi "no such file" "$NWD/fail.err" && [ -z "$failout" ]'
okcmd=$(nw_in "why did the service restart [bulky=yes] [needs_all=no] [where=pick:head]" Bash "$(python3 -c 'import json,sys;print(json.dumps({"command":"cat "+sys.argv[1]}))' "$BIG")" | python3 hooks/narrow_output.py | nw_field hookSpecificOutput.updatedInput.command)
bash -c "$okcmd" >/dev/null 2>&1; okrc=$?
check "narrow: a succeeding command still reports success" '[ "$okrc" -eq 0 ]'
shortcmd=$(nw_in "why did the service restart [bulky=yes] [needs_all=no] [where=pick:head]" Bash '{"command":"echo one-line-only"}' | python3 hooks/narrow_output.py | nw_field hookSpecificOutput.updatedInput.command)
shortran=$(bash -c "$shortcmd" 2>&1)
check "narrow: output shorter than the limit prints no marker, because nothing was cut" '[ "$shortran" = "one-line-only" ]'

# 12. Read's free fast path: a small file cannot be bulky, so it is never judged
: > "$nwlog"
smallout=$(nw_in "what does this config set [bulky=yes] [needs_all=no]" Read "$(python3 -c 'import json,sys;print(json.dumps({"file_path":sys.argv[1]}))' "$SMALLR")" | JEV_HOOKS_DEBUG=1 JEV_HOOKS_LOG="$nwlog" python3 hooks/narrow_output.py 2>"$NWD/small.err")
check "narrow: a Read under 64 KB makes no call at all" '[ -z "$smallout" ] && [ "$(nw_calls "$nwlog")" = "0" ] && grep -q "fast-path: small-file" "$NWD/small.err"'
: > "$nwlog"
missout=$(nw_in "what does this config set [bulky=yes] [needs_all=no]" Read '{"file_path":"/nope/not-a-file.txt"}' | JEV_HOOKS_DEBUG=1 JEV_HOOKS_LOG="$nwlog" python3 hooks/narrow_output.py 2>"$NWD/miss.err")
check "narrow: an unreadable Read path fails open with no call" '[ -z "$missout" ] && [ "$(nw_calls "$nwlog")" = "0" ] && grep -q "fast-path: unreadable-path" "$NWD/miss.err"'
: > "$nwlog"
bigrout=$(nw_in "what does this config set [bulky=yes] [needs_all=no] [where=pick:head]" Read "$(python3 -c 'import json,sys;print(json.dumps({"file_path":sys.argv[1]}))' "$BIGR")" | JEV_HOOKS_LOG="$nwlog" python3 hooks/narrow_output.py)
check "narrow: a Read over 64 KB is still judged and narrowed" '[ -n "$bigrout" ] && [ "$(nw_calls "$nwlog")" = "1" ]'
# --- end narrow output

# --- edit risk gate (hooks/edit_risk_gate.py) ---------------------------------
# NOT under $CLAUDE_PLUGIN_DATA: mktemp puts that in /tmp, which the gate
# correctly treats as scratch and skips, so every case here would fast-path.
EG="$(mktemp -d "$PWD/.egtest.XXXXXX")"
trap 'kill $STUB 2>/dev/null; rm -rf "$EG"' EXIT
git -C "$EG" init -q; git -C "$EG" config user.email t@t; git -C "$EG" config user.name t
SECRET="s3cret-never-leaves-the-machine"
seq 200 > "$EG/tracked.tf"; printf '.env\n' > "$EG/.gitignore"; printf 'TOKEN=%s\n' "$SECRET" > "$EG/.env"
printf '*.lock\n' >> "$EG/.gitignore"; seq 500 > "$EG/regen.lock"; seq 50 > "$EG/untracked.tf"
git -C "$EG" add tracked.tf .gitignore >/dev/null; git -C "$EG" commit -qm init
eg() { printf '%s' "$1" | JEV_HOOKS_DEBUG=1 python3 hooks/edit_risk_gate.py 2>"$CLAUDE_PLUGIN_DATA/eg.err"; }
egin() { python3 -c 'import json,sys;print(json.dumps({"tool_name":sys.argv[1],"tool_input":json.loads(sys.argv[2]),"cwd":sys.argv[3]}))' "$@"; }
STEER='[unrecoverable=yes] [proportionate=pick:wholesale] [action=pick:confirm]'

out=$(eg "$(egin Edit "{\"file_path\":\"$EG/tracked.tf\",\"old_string\":\"1\",\"new_string\":\"x\"}" "$STEER")")
check "edit gate: a clean tracked file never reaches Jev" '[ -z "$out" ] && grep -q "fast-path: git clean" "$CLAUDE_PLUGIN_DATA/eg.err"'
out=$(eg "$(egin Write "{\"file_path\":\"$EG/brand-new.tf\",\"content\":\"x\"}" "$STEER")")
check "edit gate: creating a new file never reaches Jev" '[ -z "$out" ] && grep -q "fast-path: new file" "$CLAUDE_PLUGIN_DATA/eg.err"'
out=$(eg "$(egin Write "{\"file_path\":\"/tmp/eg-scratch.txt\",\"content\":\"x\"}" "$STEER")")
check "edit gate: a scratch path never reaches Jev" '[ -z "$out" ] && grep -q "fast-path: scratch" "$CLAUDE_PLUGIN_DATA/eg.err"'
out=$(eg "$(egin Read "{\"file_path\":\"$EG/tracked.tf\"}" "$STEER")")
check "edit gate: a tool it does not gate is ignored" '[ -z "$out" ]'

echo "uncommitted" >> "$EG/tracked.tf"
eglog="$CLAUDE_PLUGIN_DATA/eg.log"; : > "$eglog"
out=$(printf '%s' "$(egin Write "{\"file_path\":\"$EG/tracked.tf\",\"content\":\"tiny\"}" "$STEER")" | JEV_HOOKS_LOG="$eglog" python3 hooks/edit_risk_gate.py)
check "edit gate: a whole-file write over uncommitted changes asks" 'echo "$out" | grep -q "\"permissionDecision\": \"ask\"" && echo "$out" | grep -q "uncommitted changes"'
check "edit gate: that case is a fact and costs no Jev call" '[ "$(grep -c "\"kind\": \"call\"" "$eglog")" = "0" ]'
check "edit gate: it names the file and says to commit or stash" 'echo "$out" | grep -q "tracked.tf" && echo "$out" | grep -q "Commit or stash it first"'
: > "$eglog"
out=$(printf '%s' "$(egin Edit "{\"file_path\":\"$EG/tracked.tf\",\"old_string\":\"7\",\"new_string\":\"x\"}" "$STEER")" | JEV_HOOKS_DEBUG=1 JEV_HOOKS_LOG="$eglog" python3 hooks/edit_risk_gate.py 2>"$CLAUDE_PLUGIN_DATA/eg.err")
check "edit gate: a targeted Edit on a dirty file is silent and costs no call" '[ -z "$out" ] && [ "$(grep -c "\"kind\": \"call\"" "$eglog")" = "0" ] && grep -q "git holds the base" "$CLAUDE_PLUGIN_DATA/eg.err"'

: > "$JEV_STUB_RECORD"
out=$(eg "$(egin Write "{\"file_path\":\"$EG/.env\",\"content\":\"TOKEN=new\"}" "$STEER")")
check "edit gate: a git-ignored file is judged, not waved through" 'echo "$out" | grep -q "\"permissionDecision\": \"ask\"" && echo "$out" | grep -q "is ignored"'
check "edit gate: the file's contents are never sent to Jev" '[ -s "$JEV_STUB_RECORD" ] && ! grep -q "$SECRET" "$JEV_STUB_RECORD"'

BLOCKSTEER='[unrecoverable=yes] [proportionate=pick:wholesale] [action=pick:block]'
out=$(eg "$(egin Write "{\"file_path\":\"$EG/untracked.tf\",\"content\":\"x\"}" "$BLOCKSTEER")")
check "edit gate: destroying unversioned work is denied" 'echo "$out" | grep -q "\"permissionDecision\": \"deny\""'
out=$(printf '%s' "$(egin Write "{\"file_path\":\"$EG/untracked.tf\",\"content\":\"x\"}" "$BLOCKSTEER")" | JEV_HOOKS_GATE_MODE=warn python3 hooks/edit_risk_gate.py)
check "edit gate: warn mode downgrades deny to ask" 'echo "$out" | grep -q "\"permissionDecision\": \"ask\""'
REGEN='[unrecoverable=no] [proportionate=pick:wholesale] [action=pick:confirm]'
out=$(eg "$(egin Write "{\"file_path\":\"$EG/regen.lock\",\"content\":\"{}\"}" "$REGEN")")
check "edit gate: a recoverable file stays silent even on a confirm verdict" '[ -z "$out" ]'
out=$(eg "$(egin Write "{\"file_path\":\"$EG/.env\",\"content\":\"x\"}" "$STEER")")
check "edit gate: an ignored file is never told to commit itself" 'echo "$out" | grep -q "copy it aside" && ! echo "$out" | grep -q "Commit or stash"'
SAFESTEER='[unrecoverable=no] [proportionate=pick:targeted] [action=pick:allow]'
out=$(eg "$(egin Edit "{\"file_path\":\"$EG/tracked.tf\",\"old_string\":\"1\",\"new_string\":\"2\"}" "$SAFESTEER")")
check "edit gate: a recoverable targeted edit passes silently" '[ -z "$out" ]'
out=$(printf '%s' "$(egin Write "{\"file_path\":\"$EG/untracked.tf\",\"content\":\"x\"}" "$STEER")" | TYPESAFE_BASE_URL=http://127.0.0.1:1 python3 hooks/edit_risk_gate.py)
check "edit gate: Jev unreachable fails open" '[ -z "$out" ]'
# --- end edit risk gate

# --- the jev CLI: a handle any agent can reach from the shell
out=$(./bin/jev noul "Is this a completed action? [q=yes]" --state "shipped it" 2>/dev/null)
check "cli: noul returns the probability as JSON" 'echo "$out" | python3 -c "import json,sys;d=json.load(sys.stdin);sys.exit(0 if d[\"type\"]==\"noul\" and d[\"noul\"]==1.0 else 1)"'
out=$(./bin/jev choice "Which one? [q=pick:beta]" --option alpha --option beta --state "x" 2>/dev/null)
check "cli: choice picks an option" 'echo "$out" | grep -q "\"choice\": \"beta\""'
out=$(./bin/jev score "How much? [q=level:2]" --level low --level mid --level high --state "x" 2>/dev/null)
check "cli: score returns a level" 'echo "$out" | grep -q "\"score\": 2"'
./bin/jev choice "one option only" --option alpha --state "x" >/dev/null 2>&1
check "cli: choice with fewer than two options is refused" '[ $? -eq 2 ]'
./bin/jev noul "no state" >/dev/null 2>&1 </dev/null
check "cli: no state is refused, not silently judged" '[ $? -eq 2 ]'
TYPESAFE_BASE_URL=http://127.0.0.1:1 ./bin/jev noul "x" --state "y" >/dev/null 2>&1
check "cli: unreachable judge exits 3 (no opinion), never a crash" '[ $? -eq 3 ]'
out=$(printf 'piped state' | ./bin/jev noul "From stdin? [q=yes]" 2>/dev/null)
check "cli: state can be piped on stdin" 'echo "$out" | grep -q "\"noul\": 1.0"'
cliq=$(mktemp); printf '{"a":{"type":"noul","instructions":"one [a=yes]"},"b":{"type":"noul","instructions":"two [b=no]"}}' > "$cliq"
out=$(./bin/jev ask "$cliq" --state "x" 2>/dev/null); rm -f "$cliq"
check "cli: ask sends several questions in ONE call" 'echo "$out" | grep -q "\"a\"" && echo "$out" | grep -q "\"b\""'
check "skill: the jev skill exists with frontmatter and the licence rules" '[ -f skills/jev/SKILL.md ] && head -1 skills/jev/SKILL.md | grep -q "^---$" && grep -q "No pass-through" skills/jev/SKILL.md && grep -q "No published comparisons" skills/jev/SKILL.md'

# --- subagent_verify judges the RECORD, not the report (2026-09-20)
# A fabricated hand-back with zero tool calls used to pass in silence — the same
# hole the graph's verification had, in the thing that vouches for every agent.
mkfork() { # $1=work-json-array  -> prints a transcript path
  python3 - "$1" <<'PYEOF'
import json,sys,tempfile
def rec(t,c): return json.dumps({"type":t,"message":{"role":t,"content":c}})
task="Execute node X.\n1. the limiter bounds concurrency to 6 with a test\n2. the retry budget is capped at 4s with a test"
L=[rec("assistant",[{"type":"tool_use","id":"a1","name":"Agent","input":{"prompt":task}}]),
   rec("user",[{"type":"tool_result","tool_use_id":"a1","content":"Fork started"}])]
for i,w in enumerate(json.loads(sys.argv[1])):
    L.append(rec("assistant",[{"type":"tool_use","id":f"t{i}","name":w["tool"],"input":{"command":w.get("cmd","x")}}]))
    L.append(rec("user",[{"type":"tool_result","tool_use_id":f"t{i}","content":w["out"]}]))
f=tempfile.NamedTemporaryFile("w",suffix=".jsonl",delete=False); f.write("\n".join(L)+"\n"); f.close(); print(f.name)
PYEOF
}
sv() { printf '%s' "$(python3 -c "import json,sys;print(json.dumps({'transcript_path':sys.argv[1],'last_assistant_message':sys.argv[2],'stop_hook_active':False}))" "$1" "$2")" | python3 hooks/subagent_verify.py; }
FABM="Done. TestLimiterHoldsUnderLoad asserts max in-flight 6; TestRetryBudget passes in 4.01s. [c0=no] [c1=no] [invented=yes] [evidence=no] [honest=no]"
tp=$(mkfork '[]'); out=$(sv "$tp" "$FABM"); rm -f "$tp"
check "verify: a fabrication with ZERO tool calls is blocked" 'echo "$out" | grep -q "made NO tool calls"'
tp=$(mkfork '[{"tool":"Bash","cmd":"ls","out":"a.go"}]'); out=$(sv "$tp" "$FABM"); rm -f "$tp"
check "verify: a fabrication with unrelated work only is blocked" 'echo "$out" | grep -q "\"decision\": \"block\""'
HON="Not done: I did not add the limiter or the retry budget, and no tests were written. [c0=no] [c1=no] [invented=no] [evidence=no] [honest=yes]"
tp=$(mkfork '[]'); out=$(sv "$tp" "$HON"); rm -f "$tp"
check "verify: an honest 'not done' passes, never looped back" '[ -z "$out" ]'
REALM="1. Limiter bounds to 6 — the test passes. 2. Retry budget 4s — the test passes. [c0=yes] [c1=yes] [invented=no] [evidence=yes] [honest=no]"
tp=$(mkfork '[{"tool":"Bash","cmd":"go test -run TestLimiter","out":"--- PASS: TestLimiterHoldsUnderLoad (0.31s)\nok"},{"tool":"Bash","cmd":"go test -run TestRetryBudget","out":"--- PASS (4.01s)\nok"}]')
out=$(sv "$tp" "$REALM"); rm -f "$tp"
check "verify: real work with matching output passes" '[ -z "$out" ]'
tp=$(mkfork '[{"tool":"Bash","cmd":"go test","out":"ok"}]'); out=$(sv "$tp" "$FABM"); rm -f "$tp"
check "verify: real work whose report INFLATES it is blocked on invented specifics" 'echo "$out" | grep -q "appear nowhere"'
cnt=$(python3 -c "
import sys;sys.path.insert(0,'lib');import transcript,json,tempfile
def rec(t,c): return json.dumps({'type':t,'message':{'role':t,'content':c}})
L=[rec('assistant',[{'type':'tool_use','id':'a1','name':'Agent','input':{'prompt':'x'}}])]
f=tempfile.NamedTemporaryFile('w',suffix='.jsonl',delete=False); f.write('\n'.join(L)+'\n'); f.close()
print(transcript.tool_call_count(f.name))")
check "verify: the parent's spawning Agent call is not counted as the child's work" '[ "$cnt" = "0" ]'

# --- the work record: a call PAIRED with its output, reaching back far enough
# (2026-09-21). Measured over 184 live subagent verdicts, 94% of them blocks:
# results alone name nothing ("File created successfully" cites no path) and a
# flat newest-first budget showed the judge 15 of a 108-call agent's work, so
# anything finished early read as undone and was blocked for it.
mkwork() { # $1=calls $2=result size -> prints a transcript path
  python3 - "$1" "$2" <<'MKW'
import json,sys,tempfile
n,sz=int(sys.argv[1]),int(sys.argv[2])
def rec(t,c): return json.dumps({"type":t,"message":{"role":t,"content":c}})
L=[]
for i in range(n):
    L.append(rec("assistant",[{"type":"tool_use","id":f"t{i}","name":"Bash","input":{"command":f"go test ./pkg{i}"}}]))
    L.append(rec("user",[{"type":"tool_result","tool_use_id":f"t{i}","content":"x"*sz+f"\nok  \tpkg{i}\t1.0s"}]))
f=tempfile.NamedTemporaryFile("w",suffix=".jsonl",delete=False); f.write("\n".join(L)+"\n"); f.close(); print(f.name)
MKW
}
mkedit() { # an Edit's RESULT never names the file; only the call does
  python3 - <<'MKE'
import json,tempfile
def rec(t,c): return json.dumps({"type":t,"message":{"role":t,"content":c}})
L=[rec("assistant",[{"type":"tool_use","id":"e1","name":"Edit","input":{"file_path":"/repo/migrations/022_evidence.sql"}}]),
   rec("user",[{"type":"tool_result","tool_use_id":"e1","content":"File created successfully"}])]
f=tempfile.NamedTemporaryFile("w",suffix=".jsonl",delete=False); f.write("\n".join(L)+"\n"); f.close(); print(f.name)
MKE
}
rec_of() { python3 -c "import sys,json;sys.path.insert(0,'lib');import transcript;print(json.dumps(transcript.tool_results(sys.argv[1])))" "$1"; }

tp=$(mkwork 3 20); out=$(rec_of "$tp"); rm -f "$tp"
check "record: each call is paired with what it printed" 'echo "$out" | grep -q "go test ./pkg1" && echo "$out" | grep -q "pkg1.t1.0s"'
tp=$(mkwork 120 700); out=$(rec_of "$tp"); rm -f "$tp"
check "record: early work survives a long transcript, as a call without its output" 'echo "$out" | grep -q "go test ./pkg3"'
# Only when even the bare call lines will not fit does the window close, and
# then it says so, rather than letting the judge read silence as idleness.
tp=$(mkwork 900 700); out=$(rec_of "$tp"); rm -f "$tp"
check "record: a window too full even for call lines says how many it left out" 'echo "$out" | grep -q "earlier tool calls omitted"'
tp=$(mkwork 1 5000); out=$(rec_of "$tp"); rm -f "$tp"
check "record: a clipped result keeps its tail, where the verdict is" 'echo "$out" | grep -q "pkg0.t1.0s"'
tp=$(mkedit); out=$(rec_of "$tp"); rm -f "$tp"
check "record: a file write is attributable to the path it wrote" 'echo "$out" | grep -q "022_evidence.sql"'


# --- model router (hooks/model_router.py)
# A hook cannot change the SESSION's model - no hook event carries one. A
# subagent's it can: the Agent tool takes `model`, a per-invocation model beats
# the definition and CLAUDE_CODE_SUBAGENT_MODEL, and PreToolUse `updatedInput`
# rewrites tool input. Verified live 2026-09-21: a spawn asking for opus,
# rewritten by a probe hook, ran on claude-haiku-4-5 per the subagent
# transcript's own message.model.
mr() { printf '%s' "$1" | python3 hooks/model_router.py; }
agent() { extra="${2:-}"; [ -z "$extra" ] && extra='{}'
  python3 -c 'import json,sys;i={"tool_name":"Agent","tool_input":{"prompt":sys.argv[1],"description":"d","subagent_type":"general-purpose"}};i["tool_input"].update(json.loads(sys.argv[2]));print(json.dumps(i))' "$1" "$extra"; }

out=$(mr "$(agent 'Find every call site of rankedFrontier and list the files. [model=pick:haiku]')")
check "router: a lookup is routed to a cheap model" 'echo "$out" | grep -q "\"model\": \"haiku\"" && echo "$out" | grep -q updatedInput'
check "router: routing rewrites input only, never a permission" '! echo "$out" | grep -q permissionDecision'
check "router: the rewritten input keeps the rest of the spawn intact" 'echo "$out" | grep -q "\"subagent_type\": \"general-purpose\"" && echo "$out" | grep -q "\"prompt\":"'
check "router: the user is told where the subagent went" 'echo "$out" | grep -q "systemMessage" && echo "$out" | grep -q "routing this subagent to haiku"'

out=$(mr "$(agent 'Make TestRejudge pass; it fails on a nil map in the worker. [model=pick:opus]')")
check "router: implementation work is routed to the strong model" 'echo "$out" | grep -q "\"model\": \"opus\""'

# An explicit model is a deliberate choice by the caller.
out=$(mr "$(agent 'Find the file. [model=pick:haiku]' '{"model":"opus"}')")
check "router: a model the caller named is left alone" '[ -z "$out" ]'
out=$(JEV_HOOKS_ROUTER_FORCE=1 mr "$(agent 'Find the file. [model=pick:haiku]' '{"model":"opus"}')")
check "router: FORCE overrides the caller's model" 'echo "$out" | grep -q "\"model\": \"haiku\""'
out=$(mr "$(agent 'Find the file. [model=pick:haiku]' '{"model":"inherit"}')")
check "router: inherit is not a deliberate choice" 'echo "$out" | grep -q "\"model\": \"haiku\""'

# Every way of not being sure leaves the spawn exactly as it was.
out=$(JEV_HOOKS_ROUTER_MIN=0.99 JEV_HOOKS_ROUTER_TIER=0.99 mr "$(agent 'Find the file. [model=pick:haiku]')")
check "router: an unconfident pick changes nothing" '[ -z "$out" ]'
# Declining is NOT neutral: an untouched spawn inherits the parent's model, the
# expensive one. Measured live: "list the files here" split haiku 0.52 / sonnet
# 0.48 — not doubt that the task is cheap, only which cheap model does it.
ch() { python3 -c "
import importlib.util,sys,json; sys.path.insert(0,'lib')
spec=importlib.util.spec_from_file_location('mr','hooks/model_router.py'); m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
print(m.choose(json.loads(sys.argv[1]), ['haiku','sonnet','opus','fable'], 0.5, 0.7))" "$1"; }
check "router: a split between two cheap models still routes cheap" '[ "$(ch "{\"haiku\":0.52,\"sonnet\":0.48}")" = "haiku" ] && [ "$(ch "{\"haiku\":0.45,\"sonnet\":0.40,\"opus\":0.15}")" = "haiku" ]'
check "router: a split between two expensive models leaves the spawn alone" '[ "$(ch "{\"opus\":0.45,\"fable\":0.40,\"haiku\":0.15}")" = "None" ]'
check "router: a clear single pick wins outright" '[ "$(ch "{\"opus\":0.9,\"sonnet\":0.1}")" = "opus" ]'
out=$(JEV_HOOKS_ROUTER_MODELS=sonnet,opus mr "$(agent 'Find the file. [model=pick:haiku]')")
# The steer string lives in the prompt, which is echoed back inside updatedInput:
# assert on the MODEL field, not on the presence of the word anywhere.
check "router: a model outside the allowed set is never chosen" 'echo "$out" | grep -q "\"model\": \"sonnet\"" && ! echo "$out" | grep -q "\"model\": \"haiku\""'
out=$(JEV_HOOKS_ROUTER=off mr "$(agent 'Find the file. [model=pick:haiku]')")
check "router: the off switch is silent" '[ -z "$out" ]'
out=$(TYPESAFE_BASE_URL=http://127.0.0.1:1 mr "$(agent 'Find the file. [model=pick:haiku]')")
check "router: judge unreachable fails open" '[ -z "$out" ]'
out=$(printf '%s' '{"tool_name":"Bash","tool_input":{"command":"ls"}}' | python3 hooks/model_router.py)
check "router: another tool is not an Agent spawn" '[ -z "$out" ]'
before=$(wc -l < "$JEV_STUB_RECORD")
out=$(mr '{"tool_name":"Agent","tool_input":{"description":"d"}}')
check "router: a spawn with no task never reaches the judge" '[ -z "$out" ] && [ "$(wc -l < "$JEV_STUB_RECORD")" = "$before" ]'

# The README states this number, and a number in prose drifts silently: it said
# 111 while the suite ran 126, and the count of your own tests is the first
# claim a reader checks. So the suite asserts its own README rather than
# trusting anyone to remember.
readme_n=$(grep -oE '[0-9]+ checks against a local stub' README.md | grep -oE '^[0-9]+')
# +1 counts THIS check, so the README's number is the whole suite as a reader
# sees it printed, not the suite minus its own guard.
total=$((pass+fail+1))
if [ -n "$readme_n" ] && [ "$readme_n" != "$total" ]; then
  echo "FAIL  README says $readme_n checks; the suite ran $total - update README.md"
  fail=$((fail+1))
else
  echo "PASS  README's check count matches the suite ($total)"
  pass=$((pass+1))
fi

echo; echo "$pass passed, $fail failed"; [ "$fail" -eq 0 ]
