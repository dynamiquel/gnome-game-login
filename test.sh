#!/usr/bin/env bash
# Run the reproducible checks. No controller, Steam or session changes needed
# for the logic tests.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

echo "== logic self-test =="
./couch-controller.py --self-test

echo
echo "== environment check (read-only) =="
./couch-controller.py --doctor || true
