#!/usr/bin/env bash
# smriti-p.sh — headless claude -p call with entity wake injected.
#
# Usage:
#   <root>/.smriti/narada-p.sh "your prompt here"
#   echo "your prompt" | <root>/.smriti/narada-p.sh
#
# Set SMRITI_ROOT to use a different entity root (default: the root this
# script is deployed in, <root>/.smriti/..). The wake is the installed smriti
# package's, run by the `python` on the PATH (python -m smriti.wake).
#
# Grabs the full wake output (identity briefing + journal + project
# context) and prepends it to the prompt before firing claude -p.

set -euo pipefail

SMRITI_ROOT="${SMRITI_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
WAKE=$(SMRITI_WAKE=1 SMRITI_ROOT="$SMRITI_ROOT" python -P -m smriti.wake)

if [[ $# -gt 0 ]]; then
    PROMPT="$*"
else
    PROMPT=$(cat)
fi

printf '%s\n\n---\n\n%s\n' "$WAKE" "$PROMPT" | claude -p
