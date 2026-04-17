#!/usr/bin/env bash
set -euo pipefail

SESSION_NAME="${1:-phase1}"
PROJECT_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"

tmux has-session -t "$SESSION_NAME" 2>/dev/null && {
  echo "tmux session already exists: $SESSION_NAME"
  exit 0
}

tmux new-session -d -s "$SESSION_NAME" -c "$PROJECT_ROOT"
tmux send-keys -t "$SESSION_NAME" "claude --setting-sources user,project,local" C-m
echo "Created tmux session: $SESSION_NAME"
