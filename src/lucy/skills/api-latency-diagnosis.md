---
skill_id: api-latency-diagnosis
name: api-latency-diagnosis
description: Diagnose and improve llama-server inference latency.
trigger: slow response, latency, tokens per second, inference slow
category: devops
tools: [
  "run_shell_command",
]
---

1) Check llama-server health. 2) Check VRAM usage. 3) Check CPU usage. 4) Review startup params. 5) Verify model quantization. 6) Check kv-cache efficiency. 7) Reduce max_tokens to 512-1024.
