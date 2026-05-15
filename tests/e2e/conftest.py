"""Conftest for schwung's own on-device E2E tests.

Tests live in ``tests/e2e/`` and require a running ``schwung-testd`` on the
target Move (default localhost:47777, tunneled via SSH from the dev machine).
The ``bus`` fixture comes from the ``pytest-schwung`` plugin (installed from
``tools/pytest-schwung``); this conftest is intentionally empty for now.
"""
