#!/usr/bin/env bash
# Run a command; if it fails, publish the tail of its output as an error annotation on the check run.
# Annotations are readable without signing in (job logs are not), so every failure explains itself.
# usage: annotate.sh "<title>" command [args...]
set -u
title=$1
shift
log=$(mktemp)
"$@" 2>&1 | tee "$log"
code=${PIPESTATUS[0]}
if [ "$code" -ne 0 ]; then
    msg=$(tail -n 40 "$log" | tr -d '\r' | sed -e 's/%/%25/g' | awk 'BEGIN { ORS = "%0A" } { print }')
    echo "::error title=${title} (exit ${code})::${msg}"
fi
rm -f "$log"
exit "$code"
