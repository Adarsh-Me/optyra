"""Optyra — GSoC issue monitor."""

import importlib.metadata

try:
    __version__ = importlib.metadata.version("optyra")
except importlib.metadata.PackageNotFoundError:  # running from a source checkout
    __version__ = "0.0.0+dev"
