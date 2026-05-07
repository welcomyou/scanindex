"""Compatibility wrapper for the old gmft_onnx_table_engine module."""

from scanindex.core.tables import gmft_onnx_table_engine as _impl

globals().update(_impl.__dict__)
