#!/usr/bin/env bash
# Claude builder, Orca milestone 3. Fixes + first live run. Task allowed, cap two, per Mor.
set -uo pipefail
cd /home/moris/orca || exit 1
unset ANTHROPIC_API_KEY ANTHROPIC_BASE_URL
/home/moris/.local/bin/claude -p "$(cat docs/briefs/milestone-3.md)" \
  --resume 7b888a7b-5e3f-4209-8d54-6e63a7e13b94 \
  --model sonnet \
  --allowedTools 'Read,Write,Edit,Bash,Task' \
  --max-turns 70 \
  --output-format json \
  > /home/moris/.hermes/cache/scratch/orca/m3.json 2> /home/moris/.hermes/cache/scratch/orca/m3.err
rc=$?
echo "exit=$rc" >> /home/moris/.hermes/cache/scratch/orca/m3.status
