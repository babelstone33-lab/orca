#!/usr/bin/env bash
# Claude, resumed session, few restrictions. Mor's standing instruction: no artificial limits.
#
# Wide open on purpose: every tool, generous turn budget, allowed to commit locally.
# Still NOT pushed: pushing is the orchestrator's job after it verifies the tree.
# The guard hook in ~/.claude/hooks/guard.sh stays active and independent of this list.
set -uo pipefail
cd /home/moris/orca || exit 1
unset ANTHROPIC_API_KEY ANTHROPIC_BASE_URL
PROMPT_FILE="${1:-docs/briefs/milestone-4.md}"
SESSION="${2:-7b888a7b-5e3f-4209-8d54-6e63a7e13b94}"
/home/moris/.local/bin/claude -p "$(cat "$PROMPT_FILE")" \
  --resume "$SESSION" \
  --model sonnet \
  --allowedTools 'Read,Write,Edit,Bash,Task,WebFetch,WebSearch,Glob,Grep,NotebookEdit,TodoWrite' \
  --max-turns 120 \
  --output-format json \
  > /home/moris/.hermes/cache/scratch/orca/lean.json 2> /home/moris/.hermes/cache/scratch/orca/lean.err
echo "exit=$?" >> /home/moris/.hermes/cache/scratch/orca/lean.status
