"""KIE Viewer — review and edit Key Information Extraction outputs.

A PySide6 desktop application for browsing and labelling Vietnamese
administrative-document KIE results produced by the KIE pipeline.

Run from the repo root with:

    python -m kie_viewer
"""

__all__ = ["main"]

from .kie_viewer import main  # re-export for `python -m kie_viewer` and library use
