#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SESSION_NAME="phase1"

sudo apt-get update
sudo apt-get install -y tmux git curl ripgrep

mkdir -p "$HOME/bin"

cat > "$HOME/.tmux.conf" <<'EOF'
set -g mouse on
set -g history-limit 50000
setw -g mode-keys vi
EOF

cat > "$HOME/bin/phase1-tmux" <<EOF
#!/usr/bin/env bash
set -euo pipefail
SESSION_NAME="${SESSION_NAME}"
PROJECT_ROOT="${PROJECT_ROOT}"

if ! command -v tmux >/dev/null 2>&1; then
  echo "tmux not found"
  exit 1
fi

if ! tmux has-session -t "\$SESSION_NAME" 2>/dev/null; then
  tmux new-session -d -s "\$SESSION_NAME" -c "\$PROJECT_ROOT"
  tmux rename-window -t "\$SESSION_NAME:1" "workspace"
fi

exec tmux attach -t "\$SESSION_NAME"
EOF

chmod +x "$HOME/bin/phase1-tmux"

echo "WSL tmux environment ready."
echo "Run: ~/bin/phase1-tmux"
