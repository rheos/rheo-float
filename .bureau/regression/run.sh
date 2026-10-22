#!/bin/sh
# .bureau/regression/run.sh: the standing gate runner. The Bureau's integration gate
# calls it as `sh <repo>/.bureau/regression/run.sh`; it also runs by hand from anywhere.
#
# It runs this project's phase-gate check, then the repo check, then any promoted
# fixtures, in order, from the repo root it sits in, and runs nothing after the first
# failure.
#
# Output is counts only: one PASS, FAIL or SKIP line per gate carrying the tool's own
# summary line (and only when that line has the expected counts shape), then a total.
# Tool output stays in memory and is never printed or written anywhere, because a
# failing test or check can echo the values it compared.
#
# Database tests use finance_test through the fixtures in tests/conftest.py, which
# refuse any DSN whose database name does not end in _test. Nothing here touches the
# live ledger.
#
# Exit: 0 = every gate passed, 1 = a gate failed, 2 = setup error.
set -eu

ROOT=$(cd "$(dirname "$0")/../.." && pwd) || exit 2
cd "$ROOT" || exit 2

if ! command -v uv >/dev/null 2>&1; then
    echo "FAIL setup: uv not found on PATH"
    exit 2
fi

echo "BUREAU-SUITE: uv run ruff check && uv run mypy --strict src tests scripts" \
    "&& uv run pytest -q && uv run python scripts/repo_check.py --tracked"

gate_pass=0
gate_fail=0
gate_skip=0

# run_gate NAME SUMMARY_PATTERN COMMAND...
run_gate() {
    gate_name=$1
    gate_pattern=$2
    shift 2
    if [ "$gate_fail" -gt 0 ]; then
        echo "SKIP not-run $gate_name"
        gate_skip=$((gate_skip + 1))
        return 0
    fi
    gate_rc=0
    gate_out=$("$@" 2>&1) || gate_rc=$?
    gate_summary=$(printf '%s\n' "$gate_out" | grep -E "$gate_pattern" | tail -n 1) || true
    gate_out=""
    [ -n "$gate_summary" ] || gate_summary="(no summary line)"
    if [ "$gate_rc" -eq 0 ]; then
        echo "PASS $gate_name: $gate_summary"
        gate_pass=$((gate_pass + 1))
    else
        echo "FAIL exit=$gate_rc $gate_name: $gate_summary"
        gate_fail=$((gate_fail + 1))
    fi
}

run_gate ruff \
    '^(All checks passed!|Found [0-9]+ errors?\.)$' \
    uv run ruff check
run_gate mypy \
    '^(Success: no issues found in [0-9]+ source files?|Found [0-9]+ errors? in [0-9]+ files? \(checked [0-9]+ source files?\))$' \
    uv run mypy --strict src tests scripts
run_gate pytest \
    '^=* *(no tests ran|[0-9]+ [a-z]+(, [0-9]+ [a-z]+)*) in [0-9.]+s( \([0-9:]+\))? *=*$' \
    uv run pytest -q
run_gate repo-check \
    '^repo check (ok: [0-9]+ tracked files? checked|failed: [0-9]+ problems? in [0-9]+ files? \(tracked\))\.$' \
    uv run python scripts/repo_check.py --tracked

# Promoted Bureau fixtures (NN-<slug>.md, docs/conventions/regression-fixtures.md in
# the Bureau) run after the gates, each command from the repo root with $ROOT exported.
# Only fixtures in the git index (git ls-files, so staged ones count) are read or run;
# an untracked file with a fixture name is reported as SKIP untracked and never read.
# A fixture passes when its command exits 0; retired, slow and <none> ones are skipped.
export ROOT
if ! fixture_index=$(git -C "$ROOT" ls-files -- .bureau/regression/); then
    echo "FAIL setup: git ls-files failed"
    exit 2
fi
# repo_check refuses any other name under .bureau/, so these names hold no spaces or
# glob characters. Two-digit fixtures run before three-digit ones.
fixture_tracked=$(
    printf '%s\n' "$fixture_index" | grep -E '^\.bureau/regression/[0-9]{2}-[A-Za-z0-9._-]+\.md$' || true
    printf '%s\n' "$fixture_index" | grep -E '^\.bureau/regression/[0-9]{3}-[A-Za-z0-9._-]+\.md$' || true
)

for fixture in "$ROOT"/.bureau/regression/[0-9][0-9]-*.md \
    "$ROOT"/.bureau/regression/[0-9][0-9][0-9]-*.md; do
    [ -e "$fixture" ] || [ -L "$fixture" ] || continue
    fixture_rel=.bureau/regression/$(basename "$fixture")
    if ! printf '%s\n' "$fixture_tracked" | grep -q -x -F -e "$fixture_rel"; then
        echo "SKIP untracked $(basename "$fixture")"
        gate_skip=$((gate_skip + 1))
    fi
done

for fixture_rel in $fixture_tracked; do
    fixture="$ROOT/$fixture_rel"
    fixture_name=$(basename "$fixture")
    if [ "$gate_fail" -gt 0 ]; then
        echo "SKIP not-run $fixture_name"
        gate_skip=$((gate_skip + 1))
        continue
    fi
    if [ -L "$fixture" ] || [ ! -f "$fixture" ]; then
        echo "FAIL not-a-regular-file $fixture_name"
        gate_fail=$((gate_fail + 1))
        continue
    fi
    if grep -q -E '^(retired|slow):' "$fixture"; then
        echo "SKIP flagged $fixture_name"
        gate_skip=$((gate_skip + 1))
        continue
    fi
    fixture_cmd=$(awk '
        /^command:[[:space:]]*\|[[:space:]]*$/ { blk = 1; next }
        blk == 1 {
            if ($0 ~ /^[[:space:]]/ || $0 == "") { sub(/^  /, ""); print; next }
            blk = 0
        }
        /^command:[[:space:]]*[^|]/ && got != 1 { sub(/^command:[[:space:]]*/, ""); print; got = 1 }
    ' "$fixture")
    case $fixture_cmd in
        "<none"*)
            echo "SKIP no-command $fixture_name"
            gate_skip=$((gate_skip + 1))
            continue
            ;;
        "")
            echo "FAIL no-command $fixture_name"
            gate_fail=$((gate_fail + 1))
            continue
            ;;
    esac
    fixture_rc=0
    sh -c "$fixture_cmd" >/dev/null 2>&1 || fixture_rc=$?
    if [ "$fixture_rc" -eq 0 ]; then
        echo "PASS $fixture_name"
        gate_pass=$((gate_pass + 1))
    else
        echo "FAIL exit=$fixture_rc $fixture_name"
        gate_fail=$((gate_fail + 1))
    fi
done

echo "---"
echo "pass=$gate_pass fail=$gate_fail skip=$gate_skip"
[ "$gate_fail" -eq 0 ] || exit 1
exit 0
