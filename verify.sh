#!/usr/bin/env bash
# verify.sh - token-dashboard - gerado pelo pipeline OpenCode em 2026-10-03
#
# =============================================================================
# PROJECT: token-dashboard (Python 3.8+, STDLIB-ONLY — zero third-party deps,
# no requirements.txt, no package manager: no deps:* checks apply).
# The only verification toolchain is the unittest suite; the syntax check is
# a fast byte-compile of the package + cli.py. The full suite is marked
# !slow (~63s — test_cli spawns REAL subprocesses) so the default batch stays
# fast; `--full` runs it too.
#
# Generated from the base template (dist/docs/verify-template.sh). Runner and
# output contract preserved verbatim — the pipeline parses "VERIFY:" lines
# machine-style. Do not change them.
#
# Output contract:
#   all executed checks pass  -> one line "VERIFY: PASSALL"              exit 0
#                                (tool-not-installed SKIPs are warnings: they
#                                count toward <total>, never toward <failed>)
#   any check failed          -> "VERIFY: FAIL <failed>/<total>"         exit 1
#                                followed by the captured error lines, each
#                                prefixed "[tipo:lang] " (the prefix is kept
#                                so tool output can never spoof a bare
#                                "VERIFY:" contract line)
#   command not found (127)   -> "VERIFY: SKIP <check> (tool not installed)"
#   --outdated, nothing found -> one line "VERIFY: OUTDATED NONE"        exit 0
#   --outdated, tool failed   -> "VERIFY: OUTDATED TOOLFAIL <eco> (command
#                                exited <code>)" lines are printed and the
#                                NONE all-clear line is SUPPRESSED (still
#                                exit 0: informational mode); manifest found
#                                but tool absent -> "VERIFY: OUTDATED SKIP
#                                <eco> (tool not installed)" (same: no NONE)
#   malformed CHECKS entry    -> "VERIFY: SKIP <entry> (invalid entry —
#                                expected tipo:lang:comando)" reported once
#                                at startup: counts toward <total>, never
#                                toward <failed>, never fails the run
# =============================================================================

# No "set -e": the batch runner must keep going after a failing check.
set -u

# CHECKS ----------------------------------------------------------------------
# Each entry is a string "tipo:lang:comando":
#   tipo    - check category (syntax, test, build, deps, ...)
#   lang    - ecosystem (ts, node, python, go, php, ...)
#   comando - the shell command; the FIRST TWO colons split tipo/lang, the
#             remainder of the string is the command (colons inside the
#             command are preserved).
# Add a trailing "!slow" to exclude a check from the default run (--full runs
# it anyway).
CHECKS=(
  "syntax:python:python3 -m compileall -q token_dashboard cli.py"
  "test:python:python3 -m unittest discover tests !slow"
)

# ARG PARSING -----------------------------------------------------------------

usage() {
  printf 'usage: %s [--full] [--outdated]\n' "${0##*/}"
  printf '  --full      also run checks marked !slow\n'
  printf '  --outdated  list outdated dependencies (informational only)\n'
}

FULL=0
MODE=checks
for arg in "$@"; do
  case "$arg" in
    --full) FULL=1 ;;
    --outdated) MODE=outdated ;;
    -h|--help) usage; exit 0 ;;
    *)
      printf 'error: unknown flag: %s\n' "$arg" >&2
      usage >&2
      exit 2
      ;;
  esac
done

# Shared scratch buffer (error lines / outdated list); cleaned on exit.
BUF=$(mktemp)
trap 'rm -f "$BUF"' EXIT

# --outdated MODE (informational) ----------------------------------------------

collect_outdated_lines() {
  # $1: ecosystem label; $2: raw command output. Appends compact lines
  # "[eco] <component>" to BUF, filtering the per-tool header/separator noise.
  # Sets the global COLLECTED to the number of lines appended: callers use it
  # to tell a failed command that produced nothing from a healthy
  # "everything up to date" empty run.
  local eco="$1" out="$2" line word
  COLLECTED=0
  while IFS= read -r line; do
    case "$line" in
      '' | -*) continue ;;
    esac
    # Header noise is dropped ONLY when the line's FIRST WORD is exactly a
    # known tool header word (i.e. followed by a space or end of line). A
    # dependency whose name merely starts with one of these words (e.g.
    # "platform-api 1.0 2.0") is kept.
    word="${line%% *}"
    case "$word" in
      Package | package | Direct | direct | Indirect | indirect | legacy | Found | found | Platform | platform)
        continue
        ;;
    esac
    printf '[%s] %s\n' "$eco" "$line"
    COLLECTED=$((COLLECTED + 1))
  done <<< "$out" >> "$BUF"
}

run_outdated() {
  # TOOLFAIL detection: the old "|| true" masked tool crashes, network errors
  # and invalid manifests as an empty "all up to date" result. Each command's
  # exit status is now captured; a non-zero exit that produced NO parseable
  # output line is treated as a tool failure. The per-tool "found outdated"
  # convention is preserved: npm exits 1 when packages ARE outdated but then
  # produces output lines, which passes the guard (exit 0 = up to date, exit
  # 1 + lines = normal, exit != 0 + NO lines = failure).
  local out status notices=''
  : > "$BUF"

  # Ecosystems are detected by manifest/lockfile presence in the current
  # directory; the outdated command only runs when the tool is installed.
  if [ -f package.json ]; then
    if command -v npm >/dev/null 2>&1; then
      out=$(npm outdated 2>/dev/null); status=$?
      collect_outdated_lines node "$out"
      if [ "$status" -ne 0 ] && [ "$COLLECTED" -eq 0 ]; then
        notices="${notices}VERIFY: OUTDATED TOOLFAIL node (command exited ${status})"$'\n'
      fi
    else
      notices="${notices}VERIFY: OUTDATED SKIP node (tool not installed)"$'\n'
    fi
  fi
  if [ -f composer.lock ]; then
    if command -v composer >/dev/null 2>&1; then
      out=$(composer outdated 2>/dev/null); status=$?
      collect_outdated_lines php "$out"
      if [ "$status" -ne 0 ] && [ "$COLLECTED" -eq 0 ]; then
        notices="${notices}VERIFY: OUTDATED TOOLFAIL php (command exited ${status})"$'\n'
      fi
    else
      notices="${notices}VERIFY: OUTDATED SKIP php (tool not installed)"$'\n'
    fi
  fi
  if [ -f requirements.txt ] || [ -f pyproject.toml ]; then
    if command -v pip >/dev/null 2>&1; then
      out=$(pip list --outdated 2>/dev/null); status=$?
      collect_outdated_lines python "$out"
      if [ "$status" -ne 0 ] && [ "$COLLECTED" -eq 0 ]; then
        notices="${notices}VERIFY: OUTDATED TOOLFAIL python (command exited ${status})"$'\n'
      fi
    else
      notices="${notices}VERIFY: OUTDATED SKIP python (tool not installed)"$'\n'
    fi
  fi
  if [ -f go.mod ]; then
    if command -v go >/dev/null 2>&1; then
      # Only modules annotated with "[latest version]" are outdated. The grep
      # runs as a separate step so $? is go's exit status, not grep's.
      out=$(go list -m -u all 2>/dev/null); status=$?
      out=$(printf '%s\n' "$out" | grep -F '[' || true)
      collect_outdated_lines go "$out"
      if [ "$status" -ne 0 ] && [ "$COLLECTED" -eq 0 ]; then
        notices="${notices}VERIFY: OUTDATED TOOLFAIL go (command exited ${status})"$'\n'
      fi
    else
      notices="${notices}VERIFY: OUTDATED SKIP go (tool not installed)"$'\n'
    fi
  fi

  # TOOLFAIL/SKIP notices mean the dependencies could not be fully verified:
  # in that case the "OUTDATED NONE" all-clear line must NOT be printed.
  # Informational mode: the run still exits 0.
  if [ -n "$notices" ]; then
    printf '%s' "$notices"
  fi
  if [ -s "$BUF" ]; then
    cat "$BUF"
  elif [ -z "$notices" ]; then
    printf 'VERIFY: OUTDATED NONE\n'
  fi
  exit 0
}

if [ "$MODE" = outdated ]; then
  run_outdated
fi

# BATCH CHECK RUNNER ------------------------------------------------------------
# Runs ALL checks (never stops at the first failure), capturing combined
# stdout+stderr per check.

total=0
failed=0

if [ "${#CHECKS[@]}" -eq 0 ]; then
  # Nothing configured (bare template): harmless success.
  printf 'VERIFY: PASSALL\n'
  exit 0
fi

# Startup validation: entries that do not split into "tipo:lang:comando" are
# reported ONCE here as a degenerate "VERIFY: SKIP" (same accounting as a
# tool-not-installed SKIP: counts toward <total>, never toward <failed>, never
# fails the run). The run loop below never fails on a malformed entry.
for entry in "${CHECKS[@]}"; do
  [ -z "$entry" ] && continue
  v_spec="$entry"
  case "$v_spec" in
    *!slow) v_spec="${v_spec%!slow}" ;;
  esac
  v_ok=0
  case "$v_spec" in
    *:*:*)
      IFS=':' read -r v_tipo v_lang v_cmd <<< "$v_spec"
      if [ -n "$v_tipo" ] && [ -n "$v_lang" ] && [ -n "$v_cmd" ]; then
        v_ok=1
      fi
      ;;
  esac
  if [ "$v_ok" -eq 0 ]; then
    total=$((total + 1))
    printf 'VERIFY: SKIP %s (invalid entry — expected tipo:lang:comando)\n' "$entry"
  fi
done

for entry in "${CHECKS[@]}"; do
  [ -z "$entry" ] && continue

  # Trailing "!slow" marker: excluded from the default run, included by --full.
  slow=0
  spec="$entry"
  case "$spec" in
    *!slow)
      slow=1
      spec="${spec%!slow}"
      ;;
  esac
  if [ "$slow" -eq 1 ] && [ "$FULL" -eq 0 ]; then
    continue
  fi

  tipo=''
  lang=''
  cmd=''
  IFS=':' read -r tipo lang cmd <<< "$spec"
  if [ -z "$tipo" ] || [ -z "$lang" ] || [ -z "$cmd" ]; then
    # Malformed entry: already counted and reported as
    # "VERIFY: SKIP <entry> (invalid entry ...)" by the startup validation
    # pass. Do not run it, do not count it twice, never let it fail the run.
    continue
  fi

  total=$((total + 1))
  output=''
  status=0
  output=$(bash -c "$cmd" 2>&1)
  status=$?

  if [ "$status" -eq 127 ]; then
    # Command not found: warn only. Counts toward <total>, never <failed>.
    printf 'VERIFY: SKIP %s (tool not installed)\n' "$spec"
    continue
  fi

  if [ "$status" -ne 0 ]; then
    failed=$((failed + 1))
    if [ -n "$output" ]; then
      # Keep the FIRST failure indication: at most 3 captured lines, each
      # printed with the "[tipo:lang] " prefix so tool output can never
      # spoof a bare "VERIFY:" contract line.
      printf '%s\n' "$output" | head -n 3 | while IFS= read -r line; do
        printf '[%s:%s] %s\n' "$tipo" "$lang" "$line"
      done >> "$BUF"
    else
      # Failing check with no captured output: the exit code is the detail.
      printf '[%s:%s] (exit %d)\n' "$tipo" "$lang" "$status" >> "$BUF"
    fi
  fi
done

if [ "$failed" -gt 0 ]; then
  printf 'VERIFY: FAIL %d/%d\n' "$failed" "$total"
  cat "$BUF"
  exit 1
fi

printf 'VERIFY: PASSALL\n'
exit 0
