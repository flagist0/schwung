"""Hardcoded constants for the ``pristine_set`` fixture.

These pin the Move-side path that the fixture replaces with a fresh
Song.abl before each test. Captured once when the user created the
test template set on the device — re-capture and update this file
if the template is ever renamed or deleted on Move (see the
DeviceFiles + fixture docs for the procedure).

Layout invariant: Move stores sets under
  ``/data/UserData/UserLibrary/Sets/{uuid}/{display_name}/Song.abl``

The ``display_name`` is whatever the user named the set in Move's UI.
We only care about it because it's part of the file path. If you
rename the set in Move, update ``TEMPLATE_DISPLAY_NAME`` below to
match — the UUID stays the same so the rest of the fixture keeps
working without a redeploy.
"""

from __future__ import annotations

from pathlib import Path


# UUID of the Move set used as the test template. Captured 2026-05-17
# from the most-recently-created set after the user added it via the
# Web UI / device. If you create a new template, find its UUID with
# `ssh ableton@move.local 'find /data/UserData/UserLibrary/Sets -name
# Song.abl -exec stat -c "%Y %n" {} \;' | sort -rn | head -n 1`.
TEMPLATE_UUID = "29cb4bd4-5762-4119-b60a-5a564a9f4ad7"

# Display name as the user named it on Move (becomes the directory
# name inside the UUID dir). Move auto-numbers new sets "Set N";
# rename in the Web UI on move.local if you want a more honest name.
TEMPLATE_DISPLAY_NAME = "Set 6"

# Where the template's Song.abl lives on the device. Computed, not
# stored, so a rename of either constant above doesn't need three
# edits.
TEMPLATE_DEVICE_SONG_PATH = (
    f"/data/UserData/UserLibrary/Sets/{TEMPLATE_UUID}/{TEMPLATE_DISPLAY_NAME}/Song.abl"
)

# Where on the device we stage the canonical template before each
# session, so per-test fixtures can `cp` from this path (local on
# device) instead of `scp` from the dev host (network). Under
# /data/UserData/schwung because that's already on the larger
# partition and we own that namespace.
DEVICE_STAGING_PATH = "/data/UserData/schwung/_test_template_song.abl"

# Where the canonical empty Song.abl lives in the repo. Resolved
# relative to this file so tests can be run from any cwd.
REPO_TEMPLATE_PATH = (
    Path(__file__).resolve().parent.parent.parent.parent.parent  # tools/.../schwung_bus → repo root
    / "tests" / "fixtures" / "empty_song.abl"
)
