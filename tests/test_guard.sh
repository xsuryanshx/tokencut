#!/bin/bash
# Offline tests for the PreToolUse guard. No model calls.
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
GUARD="$ROOT/plugins/tokencut/hooks/guard.py"
TMP="$(mktemp -d)"
export TOKENCUT_HOME="$TMP/home"
trap 'rm -rf "$TMP"' EXIT

seq 1 1000 > "$TMP/big.txt"
seq 1 10 > "$TMP/small.txt"
pass=0; fail=0

run() { # name, expect(deny|allow), session, tool, tool_input-json
  local out
  out=$(printf '{"session_id":"%s","cwd":"%s","tool_name":"%s","tool_input":%s}' "$3" "$TMP" "$4" "$5" | python3 "$GUARD")
  if [[ "$2" == deny && "$out" == *'"deny"'* ]] || [[ "$2" == allow && -z "$out" ]]; then
    pass=$((pass+1)); echo "ok   $1"
  else
    fail=$((fail+1)); echo "FAIL $1 -> ${out:0:200}"
  fi
}

run "small Read allowed"              allow s1 Read '{"file_path":"small.txt"}'
run "big Read denied"                 deny  s1 Read '{"file_path":"big.txt"}'
run "big Read retry allowed"          allow s1 Read '{"file_path":"big.txt"}'
run "big Read other session denied"   deny  s2 Read '{"file_path":"big.txt"}'
run "big Read with small limit ok"    allow s3 Read '{"file_path":"big.txt","offset":100,"limit":200}'
run "big Read with huge limit denied" deny  s3 Read '{"file_path":"big.txt","limit":5000}'
run "missing file allowed"            allow s4 Read '{"file_path":"nope.txt"}'
run "cat big denied"                  deny  s5 Bash '{"command":"cat big.txt"}'
run "cat big | grep allowed"          allow s6 Bash '{"command":"cat big.txt | grep 5"}'
run "cat big > copy allowed"          allow s6 Bash '{"command":"cat big.txt > copy.txt"}'
run "cd && cat big denied"            deny  s7 Bash '{"command":"cd /tmp && cat '"$TMP"'/big.txt"}'
run "head -n 20 allowed"              allow s8 Bash '{"command":"head -n 20 big.txt"}'
run "head -n 5000 denied"             deny  s8 Bash '{"command":"head -n 5000 big.txt"}'
run "cat small allowed"               allow s9 Bash '{"command":"cat small.txt"}'
run "heredoc allowed"                 allow s9 Bash '{"command":"cat <<EOF\nhi\nEOF"}'
run "ls allowed"                      allow s9 Bash '{"command":"ls -la"}'
TOKENCUT_DISABLE=1 run "disabled env allows" allow s10 Read '{"file_path":"big.txt"}'
TOKENCUT_MIN_LINES=5000 run "threshold env respected" allow s11 Read '{"file_path":"big.txt"}'
out=$(echo 'not json' | python3 "$GUARD"; echo "exit=$?")
[[ "$out" == "exit=0" ]] && { pass=$((pass+1)); echo "ok   bad input fails open"; } || { fail=$((fail+1)); echo "FAIL bad input -> $out"; }

echo "$pass passed, $fail failed"
[[ $fail -eq 0 ]]
