#!/usr/bin/env bash
# End-to-end exercise of every hook against the stub Jev. No key, no network.
set -u
cd "$(dirname "$0")/.."
PORT=${PORT:-18766}
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

echo; echo "$pass passed, $fail failed"; [ "$fail" -eq 0 ]
