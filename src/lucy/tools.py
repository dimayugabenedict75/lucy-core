# (Expanding the existing Toolset from the previous step)

class Toolset:
    # ... (existing tools) ...

    def run_powershell_admin(self, command: str):
        """
        Executes a command in PowerShell with elevated privileges.
        Use this for system-level changes (reboot, hardware, services).
        """
        # We use 'powershell.exe' directly with the -Command flag
        # The command is wrapped in double quotes to handle complex strings
        import subprocess
        result = subprocess.run(
            ["powershell.exe", "-Command", command],
            capture_output=True,
            text=True,
            check=True
        )
        return result.stdout

# (Add this to the primary tool list)
