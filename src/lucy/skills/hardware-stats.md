# Skill: hardware-stats
# Description: Provides a comprehensive overview of the system's hardware components (CPU, GPU, RAM, and Storage).
# Execution:
# - CPU: Run `lscpu` or `nproc`
# - GPU: Run `nvidia-smi` (if NVIDIA) or `rocm-smi` (if AMD) or `glxinfo | grep "OpenGL renderer"`
# - RAM: Run `free -h`
# - Storage: Run `df -h | grep '^/'`
# Output: A concise summary of the machine's physical power.