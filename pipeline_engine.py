"""Compatibility wrapper for the old pipeline_engine module."""

from scanindex.core.pipeline import batch_pipeline as _impl

globals().update(_impl.__dict__)
