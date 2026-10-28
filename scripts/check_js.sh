#!/bin/sh
# Syntax-check UI scripts (no JS test harness exists; this is the CI floor).
# Fails when node is present and a check fails; warns and passes when node
# is absent so environments without node still run the Python suite.
set -u
ok=0
if ! command -v node >/dev/null 2>&1; then
  echo "check_js: node not found, skipping JS syntax check" >&2
  exit 0
fi
for f in ui/app-*.js; do
  if ! node --check "$f"; then
    ok=1
  fi
done
exit $ok
