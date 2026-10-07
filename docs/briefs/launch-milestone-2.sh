#!/usr/bin/env bash
# Claude builder, Orca milestone 2. Resumes the fixed topic session. No commit, no push.
set -uo pipefail
cd /home/moris/orca || exit 1
unset ANTHROPIC_API_KEY ANTHROPIC_BASE_URL
/home/moris/.local/bin/claude -p "$(cat docs/briefs/milestone-2.md)" \
  --resume 7b888a7b-5e3f-4209-8d54-6e63a7e13b94 \
  --model sonnet \
  --allowedTools 'Read,Write,Edit,Bash' \
  --max-turns 60 \
  --output-format json \
  > /home/moris/.hermes/cache/scratch/orca/m2.json 2> /home/moris/.hermes/cache/scratch/orca/m2.err
rc=$?
echo "exit=$rc" >> /home/moris/.hermes/cache/scratch/orca/m2.status
