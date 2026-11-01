#!/usr/bin/env bash
# Install and start the free AI model (Ollama) inside a GitHub job.
# Usage: scripts/start_ai.sh MODEL_NAME     (for example qwen2.5:3b)
MODEL="${1:-qwen2.5:3b}"
fail() { echo "::warning title=Free AI model::$1"; exit 1; }
curl -fsSL https://ollama.com/install.sh -o ollama-install.sh || fail "could not download the installer"
sh ollama-install.sh > ollama.log 2>&1 || fail "install failed: $(tail -c 300 ollama.log | tr '\n' ' ')"
for i in $(seq 1 30); do curl -sf http://127.0.0.1:11434/api/tags >/dev/null && break; sleep 2; done
curl -sf http://127.0.0.1:11434/api/tags >/dev/null || fail "the AI server did not start"
ollama pull "$MODEL" > pull.log 2>&1 || fail "download of $MODEL failed: $(tail -c 300 pull.log | tr '\r\n' '  ')"
curl -sf http://127.0.0.1:11434/api/generate \
  -d "{\"model\":\"$MODEL\",\"prompt\":\"Reply with OK\",\"stream\":false}" > warm.json \
  || fail "the model did not answer a test question"
echo "Model ready: $MODEL"
