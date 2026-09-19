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

echo; echo "$pass passed, $fail failed"; [ "$fail" -eq 0 ]
