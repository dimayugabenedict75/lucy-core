---
name: hardware-stats
description: Retrieves the current system hardware specifications (CPU, GPU, RAM).
category: general
triggers: ["hardware", "processor", "CPU", "GPU", "RAM", "graphics", "memory", "specs"]
instructions: |
  When the user asks about the computer's hardware, use the terminal to fetch:
  1. CPU: Use `lscpu | grep "Model name"` or `wmic cpu | findstr "Name"`.
  2. GPU: Since we use AMD, use `rocm-smi` or `glxinfo | grep "OpenGL renderer"`.
  3. RAM: Use `free -h` (look for the total memory).
  
  Combine these into a concise summary. If the user asks about a specific component, focus on that.
