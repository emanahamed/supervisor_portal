#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────
#  run_tests.sh  –  Run the full test suite and revert DB changes
#
#  Usage:  ./run_tests.sh [pytest args...]
#  Examples:
#    ./run_tests.sh                  # run all tests
#    ./run_tests.sh -v               # verbose
#    ./run_tests.sh -k "dbs"         # only DBS tests
#    ./run_tests.sh --tb=short       # short tracebacks
# ──────────────────────────────────────────────────────────────

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# ── Paths ──
DB_FILE="instance/observations.db"
DB_BACKUP="instance/observations.db.test-backup"
TEST_DB="instance/test.db"
UPLOAD_DIR="static/uploads/dbs"

echo "═══════════════════════════════════════════════════════════"
echo "  Supervisor Test Runner"
echo "═══════════════════════════════════════════════════════════"
echo ""

# ── Step 1: Back up production database ──
if [ -f "$DB_FILE" ]; then
    cp "$DB_FILE" "$DB_BACKUP"
    echo "✓ Backed up $DB_FILE → $DB_BACKUP"
else
    echo "⚠ No production DB found at $DB_FILE (tests will create a fresh one)"
fi

# Record upload directory state before tests
UPLOADS_BEFORE=""
if [ -d "$UPLOAD_DIR" ]; then
    UPLOADS_BEFORE=$(find "$UPLOAD_DIR" -type f 2>/dev/null | sort)
fi

# ── Step 2: Run pytest ──
echo ""
echo "── Running pytest ──────────────────────────────────────"
echo ""

EXIT_CODE=0
python -m pytest tests/ "$@" || EXIT_CODE=$?

echo ""
echo "── Test run finished (exit code: $EXIT_CODE) ──────────"
echo ""

# ── Step 3: Clean up test artifacts ──

# Remove test database
if [ -f "$TEST_DB" ]; then
    rm -f "$TEST_DB"
    echo "✓ Removed test database ($TEST_DB)"
fi

# Remove any files uploaded during tests
if [ -d "$UPLOAD_DIR" ]; then
    UPLOADS_AFTER=$(find "$UPLOAD_DIR" -type f 2>/dev/null | sort)
    NEW_FILES=$(comm -13 <(echo "$UPLOADS_BEFORE") <(echo "$UPLOADS_AFTER") 2>/dev/null || true)
    if [ -n "$NEW_FILES" ]; then
        echo "$NEW_FILES" | while read -r f; do
            rm -f "$f"
            echo "  Removed test upload: $f"
        done
        echo "✓ Cleaned test-generated uploads"
    fi
fi

# ── Step 4: Restore production database ──
if [ -f "$DB_BACKUP" ]; then
    mv "$DB_BACKUP" "$DB_FILE"
    echo "✓ Restored $DB_FILE from backup"
else
    echo "⚠ No backup to restore"
fi

# Clean pytest cache
rm -rf .pytest_cache tests/__pycache__/.pytest_cache

echo ""
echo "═══════════════════════════════════════════════════════════"
if [ "$EXIT_CODE" -eq 0 ]; then
    echo "  All tests passed. Database reverted."
else
    echo "  Some tests failed (exit $EXIT_CODE). Database reverted."
fi
echo "═══════════════════════════════════════════════════════════"

exit "$EXIT_CODE"
