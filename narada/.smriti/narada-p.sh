#!/usr/bin/env bash
# narada-p.sh — headless claude -p call with Narada wake injected.
#
# Usage:
#   ~/.narada/.smriti/narada-p.sh "your prompt here"
#   echo "your prompt" | ~/.narada/.smriti/narada-p.sh
#
# Grabs the full wake output (identity + current-project memory + mirrors
# list) and prepends it to the prompt before firing claude -p. Use this
# when you want Narada's identity loaded in a one-shot headless call.

set -euo pipefail

WAKE=$(NARADA_WAKE=1 python "$HOME/.narada/.smriti/wake.py")

if [[ $# -gt 0 ]]; then
    PROMPT="$*"
else
    PROMPT=$(cat)
fi

printf '%s\n\n---\n\n%s\n' "$WAKE" "$PROMPT" | claude -p
