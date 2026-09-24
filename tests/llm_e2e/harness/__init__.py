"""Harness for the live mesa-mcp → iRODS → mesa-ducklake e2e tiers.

Modules here must stay importable without the optional ``llm-e2e`` extra
(``mcp``, ``litellm``, ``python-irodsclient`` live sessions): the offline
unit tests in ``test_harness_unit.py`` run in the default suite. Import
those packages inside functions, not at module top level.
"""
