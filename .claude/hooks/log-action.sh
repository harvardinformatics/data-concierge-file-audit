#!/usr/bin/env bash
# PostToolUse hook: append one TSV line per tool call to the action log.
#   columns: timestamp  session_id  tool  detail (command / path / url / pattern)
# Log target: audits/<run>/agent-actions.log, where <run> is the first line of
# audits/.current-run (written by the skill's preflight); otherwise
# audits/agent-actions.log. Never blocks a tool call: always exits 0.
set -u

root="${CLAUDE_PROJECT_DIR:-$(pwd)}"
audits="$root/audits"
log="$audits/agent-actions.log"
if [ -f "$audits/.current-run" ]; then
    run="$(head -n1 "$audits/.current-run" | tr -cd 'A-Za-z0-9._-')"
    if [ -n "$run" ] && [ -d "$audits/$run" ]; then
        log="$audits/$run/agent-actions.log"
    fi
fi

input="$(cat)"
line="$(printf '%s' "$input" | jq -r --arg ts "$(date -Iseconds)" '
    (.tool_input // {}) as $t
    | [ $ts,
        (.session_id // "-"),
        (.tool_name // "-"),
        ( if $t.command then $t.command
          elif $t.file_path then $t.file_path
          elif $t.notebook_path then $t.notebook_path
          elif $t.url then $t.url
          else ([$t.pattern, $t.path] | map(select(. != null)) | join(" in "))
          end
          | tostring | gsub("[\t\n\r]"; " ") | .[0:2000] )
      ] | @tsv' 2>/dev/null)"
[ -n "$line" ] || line="$(date -Iseconds)	-	unparsed	-"

printf '%s\n' "$line" >> "$log" 2>/dev/null
exit 0
