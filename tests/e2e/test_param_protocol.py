"""Protocol-level error paths for SET_PARAM / GET_PARAM family.

Focused on the parser and validation surface, not on the param
semantics themselves (those are covered by ion's own E2E tests which
load known modules and exercise real param round-trips).

Targets the error cases qa-edges flagged when the param commands
landed:
  - empty key, key-too-long, missing value
  - '=' in key (silent-routing-miss hazard)
  - spaces / whitespace in key (line-protocol mangling)
  - file-not-found, non-regular-file, oversized file

Doesn't load any module — these tests run with whatever the device
happens to have loaded. They exercise the daemon-side parsing and
won't even reach a DSP for the negative-case asserts. The positive-
case round-trip tests live under each module's own test directory
(e.g. ion's tests/e2e/test_*).
"""

from __future__ import annotations

import pytest

from schwung_bus import SchwungBusError


def test_set_param_rejects_empty_key(bus):
    """``SET_PARAM <space>value`` — empty key before the first space.
    Common typo (extra leading space) that would otherwise propagate
    as a key=" " into shim routing and silently miss every prefix."""
    with pytest.raises(SchwungBusError):
        bus._request("SET_PARAM  value")


def test_set_param_rejects_missing_value(bus):
    """``SET_PARAM key`` with no value at all — daemon must reject;
    chain DSPs would otherwise see set("key", "") which a few keys
    interpret as "clear to default" silently."""
    with pytest.raises(SchwungBusError):
        bus._request("SET_PARAM key")


def test_set_param_rejects_equals_in_key(bus):
    """Keys never contain '=' in any ion / chain / shim handler. A
    SET_PARAM with '=' in the key has historically been a typo for
    ``SET_PARAM key value`` accidentally written as ``SET_PARAM key=value``;
    daemon must reject so it surfaces loudly, not via a silently-
    routed set that just no-ops."""
    with pytest.raises(SchwungBusError):
        bus._request("SET_PARAM track.0.channel=5 ignored")


def test_set_param_rejects_too_long_key(bus):
    """SHADOW_PARAM_KEY_LEN is 64 bytes (including NUL). 100-char key
    overflows the SHM cap."""
    long_key = "a" * 100
    with pytest.raises(SchwungBusError):
        bus._request(f"SET_PARAM {long_key} v")


def test_get_param_rejects_key_with_space(bus):
    """GET_PARAM keys never contain spaces (param namespace is
    dot-separated). A space in the key would be split off as the
    start of an unwanted arg."""
    with pytest.raises(SchwungBusError):
        bus._request("GET_PARAM foo bar")


def test_get_param_rejects_empty_args(bus):
    with pytest.raises(SchwungBusError):
        bus._request("GET_PARAM")


def test_set_param_file_rejects_missing_path(bus):
    """SET_PARAM_FILE with a key but no path argument."""
    with pytest.raises(SchwungBusError):
        bus._request("SET_PARAM_FILE track.0.channel")


def test_set_param_file_rejects_nonexistent_file(bus):
    """File path that doesn't exist on Move's filesystem."""
    with pytest.raises(SchwungBusError):
        bus._request(
            "SET_PARAM_FILE foo.key /data/UserData/schwung/_definitely_missing_xyz.bin"
        )


def test_set_param_file_rejects_non_regular_file(bus):
    """A directory path is not a regular file. Used to make sure
    SET_PARAM_FILE doesn't hang trying to read a directory or a FIFO."""
    with pytest.raises(SchwungBusError):
        bus._request("SET_PARAM_FILE foo.key /data/UserData")


def test_dump_param_file_rejects_missing_path(bus):
    """DUMP_PARAM_FILE with a key but no path argument."""
    with pytest.raises(SchwungBusError):
        bus._request("DUMP_PARAM_FILE foo.key")


def test_set_param_client_rejects_newline_in_value(bus):
    """Python wrapper must reject newlines client-side — the
    line-based protocol would otherwise split the command into two
    lines and the second would be parsed as a separate verb."""
    with pytest.raises(ValueError):
        bus.set_param("foo.key", "line1\nline2")


def test_set_param_client_rejects_whitespace_in_path(bus):
    """Python wrapper must reject tabs / newlines in Move paths —
    the daemon's args parser only splits on space, so a tab would
    silently mangle the key/path split."""
    with pytest.raises(ValueError):
        bus.set_param_from_file("foo.key", "/data/path\twith\ttabs.json")
