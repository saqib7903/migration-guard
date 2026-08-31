# Compatibility shim.
# The project is configured entirely in pyproject.toml. This file exists so that
# `pip install -e .` also works on pip < 21.3, which cannot do PEP 660 editable
# installs from pyproject alone -- the version shipped with macOS system Python.
from setuptools import setup

setup()
