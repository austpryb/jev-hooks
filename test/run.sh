#!/usr/bin/env bash
# End-to-end exercise of every hook against the stub Jev. No key, no network.
set -u
cd "$(dirname "$0")/.."
PORT=${PORT:-$(python3 -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1])')}
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
t0=$(date +%s%N); out=$(ld "$(ev ld1 Bash 'go build ./...' 'ok' 0)"); out2=$(ld "$(ev ld1 Read 'x.go' 'contents' 0)"); t1=$(date +%s%N)
ms=$(( (t1 - t0) / 2000000 ))
check "loop: no-repeat fast path is silent" '[ -z "$out" ] && [ -z "$out2" ]'
check "loop: fast path under 150 ms (measured ${ms} ms per call)" '[ "$ms" -lt 150 ]'
check "loop: state keeps at most 6 pairs" 'for i in 1 2 3 4 5 6 7; do ld "$(ev ld1 Bash "cmd$i" out 0)" >/dev/null; done; python3 -c "import json,sys;d=json.load(open(sys.argv[1]));sys.exit(0 if len(d[\"pairs\"])==6 else 1)" "$CLAUDE_PLUGIN_DATA/loops/ld1.json"'
rm -rf "$CLAUDE_PLUGIN_DATA/loops"
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

echo; echo "$pass passed, $fail failed"; [ "$fail" -eq 0 ]
