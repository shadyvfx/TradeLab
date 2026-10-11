#!/bin/bash
# Starts the TradeLab demo on this Mac: the website, sign-in, guest limits and the AI tutor.
#
# Double-click this file in Finder. Leave the window that opens running during
# the demo; press Control-C or close the window to stop TradeLab.
#
# The AI model runs on another computer with llama-server. This finds it on
# your network (any computer answering on port 8080), or set it yourself in
# ~/.tradelab-demo/model-url.txt, for example: http://192.168.1.23:8080
#
# It runs TradeLab on its production server (serve.py). The first run, and
# the first run after requirements.txt changes, installs the server's packages
# into ~/.tradelab-demo/venv. Nothing is installed anywhere else.

cd "$(dirname "$0")" || exit 1
DEMO_HOME="$HOME/.tradelab-demo"
VENV="$DEMO_HOME/venv"
MODEL_FILE="$DEMO_HOME/model-url.txt"
LOG="$DEMO_HOME/server.log"
PORT="${TRADELAB_PORT:-5001}"
MODEL_PORT=8080
SERVER_PID=""

step() { printf '\n\033[1m%s\033[0m\n' "$*"; }
ok() { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }
fail() {
  printf '  \033[31m✗\033[0m %s\n' "$*"
  printf '\nTradeLab did not start. Press Return to close this window.'
  read -r _
  exit 1
}

stop_server() {
  if [ -n "$SERVER_PID" ]; then
    kill "$SERVER_PID" 2>/dev/null
    wait "$SERVER_PID" 2>/dev/null
    SERVER_PID=""
    printf '\nTradeLab stopped.\n'
  fi
}
trap 'stop_server; exit 0' INT TERM HUP
trap 'stop_server' EXIT

mkdir -p "$DEMO_HOME"
printf '\033[1mTradeLab demo\033[0m\n'

# -- 1. Python and packages ---------------------------------------------------

step "1. Checking Python"
PYTHON="$(command -v python3)"
if [ -z "$PYTHON" ] || ! "$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
  fail "TradeLab needs Python 3.9 or newer. Install it from https://www.python.org/downloads/ and double-click this file again."
fi
ok "$("$PYTHON" --version 2>&1)"

if [ ! -x "$VENV/bin/python" ]; then
  printf '  Setting up a private Python environment (first run only)...\n'
  "$PYTHON" -m venv "$VENV" || fail "Couldn't create $VENV."
fi
WANTED="$(shasum -a 256 requirements.txt | awk '{print $1}')"
if [ "$(cat "$DEMO_HOME/requirements.sha256" 2>/dev/null)" != "$WANTED" ]; then
  printf '  Installing the server packages (first run only, needs internet)...\n'
  "$VENV/bin/python" -m pip install --disable-pip-version-check -q -r requirements.txt \
    || fail "Installing packages failed. Check your internet connection and try again."
  printf '%s' "$WANTED" > "$DEMO_HOME/requirements.sha256"
fi
ok "Packages ready"

# -- 2. The AI model -----------------------------------------------------------

model_ok() { curl -s -m 2 "$1/health" 2>/dev/null | grep -q '"ok"'; }

step "2. Finding the AI model"
MODEL=""
if [ -n "$TRADELAB_MODEL_URL" ]; then
  MODEL="${TRADELAB_MODEL_URL%/}"
elif [ -s "$MODEL_FILE" ]; then
  MODEL="$(head -n 1 "$MODEL_FILE" | tr -d '[:space:]')"
  MODEL="${MODEL%/}"
fi
if [ -n "$MODEL" ] && ! model_ok "$MODEL"; then
  warn "The model isn't answering at $MODEL. Searching your network instead."
  MODEL=""
fi

if [ -z "$MODEL" ] && model_ok "http://127.0.0.1:$MODEL_PORT"; then
  MODEL="http://127.0.0.1:$MODEL_PORT"
fi

if [ -z "$MODEL" ]; then
  IFACE="$(route -n get default 2>/dev/null | awk '/interface:/ {print $2}')"
  MY_IP=""
  for candidate in "$IFACE" en0 en1; do
    [ -n "$candidate" ] && MY_IP="$(ipconfig getifaddr "$candidate" 2>/dev/null)"
    [ -n "$MY_IP" ] && break
  done
  PREFIX="${TRADELAB_SCAN_PREFIX:-${MY_IP%.*}}"
  if [ -z "$PREFIX" ]; then
    warn "This Mac isn't connected to a network, so it can't reach the model."
  else
    printf '  Looking for llama-server on %s.x (port %s)...\n' "$PREFIX" "$MODEL_PORT"
    FOUND="$(seq 1 254 | xargs -P 64 -I{} sh -c \
      "curl -s -m 2 http://$PREFIX.{}:$MODEL_PORT/health 2>/dev/null | grep -q '\"ok\"' && echo $PREFIX.{}" \
      | head -n 1)"
    if [ -n "$FOUND" ]; then
      MODEL="http://$FOUND:$MODEL_PORT"
    fi
  fi
fi

if [ -z "$MODEL" ]; then
  warn "Couldn't find the AI model on your network."
  GATEWAY="$(route -n get default 2>/dev/null | awk '/gateway:/ {print $2}')"
  if [ -n "$GATEWAY" ] && curl -sS -m 2 -o /dev/null "http://$GATEWAY/" 2>&1 | grep -q "No route to host"; then
    warn "macOS is blocking Terminal from your local network. Open System Settings > Privacy & Security > Local Network, turn on Terminal, then double-click this file again."
  fi
  printf '    On the computer running the model, start llama-server with --host 0.0.0.0\n'
  printf '    (not --host 127.0.0.1) so this Mac can reach it, allow it through that\n'
  printf '    computer'"'"'s firewall, and keep both computers on the same Wi-Fi.\n'
  if [ -t 0 ]; then
    printf '\n    If you know that computer'"'"'s IP address, type it and press Return\n'
    printf '    (for example 192.168.1.23). Or just press Return to go on without the tutor.\n    > '
    ENTERED=""
    read -r -t 120 ENTERED
    ENTERED="$(printf '%s' "$ENTERED" | tr -d '[:space:]')"
    if [ -n "$ENTERED" ]; then
      case "$ENTERED" in http://* | https://*) ;; *) ENTERED="http://$ENTERED" ;; esac
      case "${ENTERED#*://}" in *:*) ;; *) ENTERED="$ENTERED:$MODEL_PORT" ;; esac
      if model_ok "$ENTERED"; then
        MODEL="$ENTERED"
      else
        warn "Nothing answered at $ENTERED. Check that llama-server is running there with --host 0.0.0.0."
      fi
    fi
  fi
fi

if [ -n "$MODEL" ]; then
  printf '%s\n' "$MODEL" > "$MODEL_FILE"
  ok "Found the model at $MODEL"
else
  warn "Going on without the AI model: the site will open, but the tutor won't answer."
  printf '    Fix the model computer, then double-click this file again.\n'
fi

# -- 3. The TradeLab server ----------------------------------------------------

step "3. Starting TradeLab"
# A TradeLab server left running from an earlier start is restarted, so it
# uses the model found above.
for pid in $(lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t 2>/dev/null); do
  if ps -p "$pid" -o command= 2>/dev/null | grep -Eq "serve\.py|flask --app backend\.app"; then
    printf '  Restarting the TradeLab server that was already running...\n'
    kill "$pid" 2>/dev/null
    for _ in 1 2 3 4 5 6 7 8 9 10; do
      kill -0 "$pid" 2>/dev/null || break
      sleep 0.3
    done
  fi
done
while [ "$PORT" -lt 5011 ]; do
  CODE="$(curl -s -m 2 -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/" 2>/dev/null)"
  [ "$CODE" = "000" ] && break
  PORT=$((PORT + 1))  # something else uses this port; try the next one
done
SITE="http://127.0.0.1:$PORT"

if [ -n "$MODEL" ]; then
  export TRADELAB_LLAMA_URL="$MODEL/v1/chat/completions"
fi
# The production server (serve.py): the same one used for HTTPS and hosting.
"$VENV/bin/python" serve.py --host 127.0.0.1 --port "$PORT" > "$LOG" 2>&1 &
SERVER_PID=$!

# /api/health answers once the server is up; serve.py has opened the user
# database by then. (Polling /api/session would start a guest session each time.)
for _ in $(seq 1 40); do
  if curl -s -m 1 "$SITE/api/health" 2>/dev/null | grep -q '"ok"'; then
    break
  fi
  if ! kill -0 "$SERVER_PID" 2>/dev/null; then
    SERVER_PID=""
    printf '\n'
    tail -n 15 "$LOG"
    fail "The TradeLab server stopped while starting (details above)."
  fi
  sleep 0.5
done
curl -s -m 2 "$SITE/api/health" 2>/dev/null | grep -q '"ok"' || fail "TradeLab didn't start. See $LOG for details."
ok "Website and sign-in are running at $SITE"

# -- 4. A test question for the model -------------------------------------------
# Asked straight to llama-server: TradeLab's tutor only answers signed-in
# accounts, and this checks the part most likely to fail, the network path to
# the model computer.

if [ -n "$MODEL" ]; then
  step "4. Asking the model a test question"
  printf '  (the first answer can take a little while)\n'
  ANSWER="$(curl -s -m 150 -X POST "$MODEL/v1/chat/completions" -H 'Content-Type: application/json' \
    -d '{"messages":[{"role":"user","content":"In one sentence, what is a stock?"}],"max_tokens":80,"temperature":0.4,"chat_template_kwargs":{"enable_thinking":false}}')"
  "$VENV/bin/python" - "$ANSWER" <<'PY'
import json
import re
import sys

try:
    data = json.loads(sys.argv[1])
    text = data["choices"][0]["message"]["content"] or ""
except (ValueError, KeyError, IndexError, TypeError):
    print("  \033[33m!\033[0m The model didn't answer the test question in time. The tutor may be slow or unavailable.")
    sys.exit(0)
text = " ".join(re.sub(r"<think>.*?</think>", "", text, flags=re.S).split())
print("  \033[32m\u2713\033[0m The model answered: " + (text[:120] + "..." if len(text) > 120 else text))
print("    Sign in on the site to use the tutor: it answers signed-in accounts only.")
PY
fi

if [ ! -s knowledge_base/index.json ]; then
  printf '\n  Note: the tutor'"'"'s knowledge library isn'"'"'t on this Mac, so its answers won'"'"'t cite\n'
  printf '  sources. To add it, copy knowledge_base/index.json and knowledge_base/sources\n'
  printf '  from the other computer into this folder'"'"'s knowledge_base folder.\n'
fi

[ "$(uname)" = "Darwin" ] && open "$SITE"
printf '\n\033[1mTradeLab is open at %s\033[0m\n' "$SITE"
printf 'Leave this window open during your demo. To stop, press Control-C or close it.\n'
printf 'Server log: %s\n' "$LOG"
wait "$SERVER_PID"
SERVER_PID=""
