"""Compatibility wrapper for the old docling_tableformer_v1_onnx_engine module."""

from scanindex.core.tables import docling_tableformer_v1_onnx_engine as _impl

globals().update(_impl.__dict__)
