#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/../.."

bin="build/tests/test_sysex_reassemble"
mkdir -p "$(dirname "$bin")"

cc -std=c11 -Wall -Wextra -Werror \
  -Isrc \
  tests/host/test_sysex_reassemble.c \
  src/host/sysex_reassemble.c \
  -o "$bin"

"$bin"
