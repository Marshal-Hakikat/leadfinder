#!/usr/bin/env bash
# Install and start the free AI model (Ollama) inside a GitHub job.
# Usage: scripts/start_ai.sh MODEL_NAME     (for example qwen2.5:3b)
MODEL="${1:-qwen2.5:3b}"
fail() { echo "::warning title=Free AI model::$1"; exit 1; }
curl -fsSL https://ollama.com/install.sh -o ollama-install.sh || fail "could not download the installer"
sh ollama-install.sh > ollama.log 2>&1 || fail "install failed: $(tail -c 300 ollama.log | tr '\n' ' ')"
up() { curl -sf http://127.0.0.1:11434/api/tags >/dev/null; }
for i in $(seq 1 15); do up && break; sleep 2; done
if ! up; then                      # the installer did not start it: start it ourselves
  nohup ollama serve > ollama-serve.log 2>&1 &
  for i in $(seq 1 30); do up && break; sleep 2; done
fi
up || fail "the AI server did not start: $(tail -c 300 ollama-serve.log 2>/dev/null | tr '\n' ' ')"
ollama pull "$MODEL" > pull.log 2>&1 || fail "download of $MODEL failed: $(tail -c 300 pull.log | tr '\r\n' '  ')"
curl -sf http://127.0.0.1:11434/api/generate \
  -d "{\"model\":\"$MODEL\",\"prompt\":\"Reply with OK\",\"stream\":false}" > warm.json \
  || fail "the model did not answer a test question"
echo "Model ready: $MODEL"
