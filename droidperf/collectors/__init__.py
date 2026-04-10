"""
droidperf.collectors
--------------------
Sub-package containing individual metric collectors (memory, CPU, battery).

Each collector module exposes a single public function that accepts a
device_id and returns a typed dict of parsed metric values.
"""
