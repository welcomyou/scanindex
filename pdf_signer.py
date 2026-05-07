"""Compatibility wrapper for the old pdf_signer module."""

from scanindex.core.pdf import signer as _impl

globals().update(_impl.__dict__)
