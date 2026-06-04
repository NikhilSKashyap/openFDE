"""Minimal setup.py for pip install -e . on pip < 21.3.

All authoritative metadata lives in pyproject.toml.
Name and version are repeated here so older setuptools does not produce
an UNKNOWN.egg-info artifact.
"""

from setuptools import setup

setup(name="openfde", version="0.3.0")
