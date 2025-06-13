# run_simulation.py
import micropython_sim  # This import initializes and injects all the mocks
import time
print("RUNNER: Mocks initialized. Importing main.py to start the application...")

# Importing main will execute its top-level code, which should start the asyncio loop
# via the mocked _thread.start_new_thread.
import main

print("RUNNER: main.py has been imported. Async tasks should be running.")
print("RUNNER: Press Ctrl+C to stop the simulation.")

try:
    # Keep the main thread of this runner script alive.
    # The asyncio loop is running in a daemon thread started by the mocked _thread.
    while True:
        time.sleep(1)
except KeyboardInterrupt:
    print("\nRUNNER: Simulation interrupted by user.")
finally:
    print("RUNNER: Simulation finished.")
    # Attempt to gracefully stop the asyncio loop
    # Accessing uasyncio._loop directly is specific to this mock implementation
    if hasattr(micropython_sim, 'uasyncio') and \
       hasattr(micropython_sim.uasyncio, '_loop') and \
       micropython_sim.uasyncio._loop and \
       micropython_sim.uasyncio._loop.is_running():
        print("RUNNER: Requesting asyncio loop to stop...")
        micropython_sim.uasyncio._loop.call_soon_threadsafe(micropython_sim.uasyncio._loop.stop)
        # Give it a moment to process the stop, though daemon threads will exit anyway.
        time.sleep(0.1)
