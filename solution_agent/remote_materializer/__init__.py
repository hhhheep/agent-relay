"""Low-cost remote materialization pipeline.

This package is intentionally separate from the scheduler/registry path.  The
first version materializes task artifacts and validates them without launching
training, mutating the registry, or depending on Codex.
"""

