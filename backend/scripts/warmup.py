"""Replay "last week's" queries through the REAL pipeline before the demo.

Reads tests/cases/warmup.json, sets the simulated clock to each entry's date,
and calls the query_hr handler. Never writes to `releases` directly.
"""

if __name__ == "__main__":
    raise NotImplementedError
