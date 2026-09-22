---
skill_id: model-not-loading
name: model-not-loading
description: Diagnose and fix llama-server model loading failures.
trigger: llama-server not loading
category: devops
tools: [
  "run_shell_command",
]
---

1. Check llama-server health with curl. 2. Check port 8085 with netstat. 3. Read llama-server log. 4. Verify model file exists. 5. Check VRAM usage. 6. Restart llama-server. 7. Verify models endpoint.
