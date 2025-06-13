import time as _time # Use _time to avoid conflict if we define a time() function

# Constants for simulating MicroPython's ticks_ms behavior.
# These values can vary by MicroPython port. We'll use a common 30-bit counter.
_TICKS_PERIOD = 1 << 30  # The period of the ticks counter
_TICKS_HALF_PERIOD = _TICKS_PERIOD // 2 # Half the period, used in ticks_diff

# A base timestamp (in nanoseconds) to make simulated tick values smaller
# and potentially wrap around sooner during short simulation runs, aiding in testing.
_TICKS_START_NS = _time.monotonic_ns()

def ticks_ms():
    """
    Return a millisecond counter that wraps around, similar to MicroPython's utime.ticks_ms().
    The counter wraps every _TICKS_PERIOD milliseconds.
    """
    delta_ns = _time.monotonic_ns() - _TICKS_START_NS
    return (delta_ns // 1_000_000) % _TICKS_PERIOD

def ticks_us():
    """
    Return a microsecond counter that wraps around.
    Uses the same _TICKS_PERIOD for simplicity in this simulation.
    """
    delta_ns = _time.monotonic_ns() - _TICKS_START_NS
    return (delta_ns // 1_000) % _TICKS_PERIOD

def ticks_cpu():
    """
    Return a CPU tick counter (simulated).
    In this simulation, it's based on nanoseconds and wraps with _TICKS_PERIOD.
    Actual CPU ticks are hardware-dependent.
    """
    delta_ns = _time.monotonic_ns() - _TICKS_START_NS
    return delta_ns % _TICKS_PERIOD

def ticks_add(ticks, delta):
    """
    Add a delta to a tick value, handling wraparound.
    'ticks' and 'delta' are assumed to be in the same unit (e.g., milliseconds).
    The result is (ticks + delta) % _TICKS_PERIOD.
    """
    return (ticks + int(delta)) % _TICKS_PERIOD

def ticks_diff(new_ticks, old_ticks):
    """
    Calculate the difference between two tick values (new_ticks - old_ticks),
    correctly handling wraparound. The result represents the shortest signed
    difference and will be in the range [-TICKS_HALF_PERIOD, TICKS_HALF_PERIOD - 1].
    """
    diff = (new_ticks - old_ticks + _TICKS_HALF_PERIOD) % _TICKS_PERIOD - _TICKS_HALF_PERIOD
    return diff

# --- Standard time functions, re-exported or implemented for compatibility ---

def sleep(seconds):
    """Suspend execution for the given number of seconds (can be float)."""
    _time.sleep(float(seconds))

def sleep_ms(ms):
    """Delay execution for a given number of milliseconds (integer)."""
    if ms < 0:
        ms = 0
    _time.sleep(ms / 1000.0)

def sleep_us(us):
    """Delay execution for a given number of microseconds (integer)."""
    if us < 0:
        us = 0
    _time.sleep(us / 1_000_000.0)

def time():
    """Return the number of seconds since the Epoch (1970-01-01)."""
    return int(_time.time())

def time_ns():
    """Return the number of nanoseconds since the Epoch (1970-01-01)."""
    return _time.time_ns()

def monotonic():
    """Return a monotonic clock value (in seconds, float) that cannot go backwards."""
    return _time.monotonic()

def monotonic_ns():
    """Return a monotonic clock value (in nanoseconds, int) that cannot go backwards."""
    return _time.monotonic_ns()

def localtime(secs=None):
    """Convert seconds since Epoch to a time tuple (local time)."""
    return _time.localtime(secs if secs is not None else _time.time())

def mktime(tuple_time):
    """Convert a time tuple (local time) to seconds since Epoch."""
    return int(_time.mktime(tuple_time))

def gmtime(secs=None):
    """Convert seconds since Epoch to a time tuple (UTC)."""
    return _time.gmtime(secs if secs is not None else _time.time())