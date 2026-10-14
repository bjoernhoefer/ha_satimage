"""One memory-heavy operation at a time, including HTTP media requests."""

import threading

# Reentrant: scheduler runs hold this lock while starting the render subprocess.
HEAVY_WORK_LOCK = threading.RLock()
