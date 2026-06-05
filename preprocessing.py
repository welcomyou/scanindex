"""Compatibility wrapper for legacy imports.

Older worker code imported ``preprocessing`` as a top-level module. Runtime
code now lives under ``scanindex.core.preprocessing.preprocessing``.
"""

from scanindex.core.preprocessing.preprocessing import *  # noqa: F401,F403
