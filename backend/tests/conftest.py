"""
conftest.py — pytest configuration and shared fixtures for the Parsy test suite.
"""
import sys
import os

# Ensure the backend package is on the path regardless of CWD
_backend_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _backend_dir not in sys.path:
    sys.path.insert(0, _backend_dir)


import pytest
from logging_config import configure_logging

# Configure quiet logging during tests
configure_logging(level="WARNING", fmt="console", force=True)
