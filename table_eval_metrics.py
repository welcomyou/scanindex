"""Compatibility wrapper for the old table_eval_metrics module."""

from scanindex.core.tables import eval_metrics as _impl

globals().update(_impl.__dict__)
