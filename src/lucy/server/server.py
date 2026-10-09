"""
Lucy Core Startup Script.

This script launches the Lucy Core API server and configures it
to be accessible from your Tailscale network.
"""

import sys
import os
import logging
from pathlib import Path

# --- Critical: Insert Lucy_Core src BEFORE any other path so that
# the Hermes venv's editable install of 'lucy' (agents-harness)
# does not shadow our package. ---
LUCY_CORE_SRC = str(Path(__file__).resolve().parents[2])  # <root>/src
if LUCY_CORE_SRC not in sys.path:
    sys.path.insert(0, LUCY_CORE_SRC)

# Force-import the Lucy Core modules explicitly to lock in the right path
from lucy.server.api import app  # noqa: E402
import uvicorn  # noqa: E402

# --- Server Configuration ---
# Bind to 0.0.0.0 to listen on ALL network interfaces,
# including your Tailscale IP. This makes the UI accessible
# from https://<your-tailscale-ip>:8090
HOST = "0.0.0.0"
PORT = 8090

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger("lucy.core.server")
    logger.info(f"Starting Lucy Core API on {HOST}:{PORT}")

    # Run the server
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
