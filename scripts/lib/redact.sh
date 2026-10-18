#!/usr/bin/env bash
# Secret redaction for the operator-facing scripts (PLAN.md §11.6; spec §6 "Privacidad").
#
# Sourced, never executed. `redact_into` is the single place a command line or an error message is
# made safe to print, and it exists because of two real leaks this project has already had the
# shape of:
#
#   * Phase 1: the Telegram client builds `https://api.telegram.org/bot<TOKEN>/<method>` — the
#     token is in the **URL**, so any library that echoes the request URL in an exception message
#     (httpx does) puts the token in a log line. No unit test caught it; a real log line did.
#   * A deploy script that prints every command it will run has the same failure mode by
#     construction: print a command that carries a secret and the secret is on the operator's
#     terminal, in their scrollback and in any CI log that captured stdout.
#
# -------------------------------------------------------------------------------------------
#  What it actually does — no more, no less
# -------------------------------------------------------------------------------------------
#
# **Layer 1: exact values we were told about.** Everything in `TAA_REDACT` (space-separated) and
# everything pushed onto `SECRET_VALUES` with `register_secret_value` is removed by **literal**
# substitution. Literal matters: an earlier version built a *glob* from the value, so a value
# containing `[`, `]` or a newline survived untouched while the comment claimed otherwise.
#
# **Layer 2: shapes that are credentials wherever they appear.** A Telegram bot token
# (`<6+ digits>:<30+ chars>`), a bearer token, and a PEM private-key block — **header, body and
# footer, all three replaced**. Two earlier versions were wrong here: the first redacted only the
# header and left the base64 body; the second left the footer, and shared one iteration budget
# between its two loops, so text with many blocks stopped being redacted part-way through.
#
# **What it does not do.** It cannot redact a secret it has never seen and that has no credential
# shape — an arbitrary password, say, printed by a command we did not write. That is why the
# scripts themselves never *hold* a value: `gcloud` reads them from `--data-file=` or prompts for
# them, so they are never arguments of ours, never in shell history and never in `ps`. This file is
# the belt to those braces, not the braces.
#
# `TAA_REDACT` and `register_secret_value` are populated by the tests and by any future caller that
# has had to load a value; `deploy.sh` deliberately never does, so on its own path layer 1 is
# empty and layer 2 carries the weight.

REDACT_PLACEHOLDER="<redacted>"
SECRET_VALUES=()
REDACTED_TEXT=""
RENDERED_COMMAND=""

register_secret_value() {
  [[ -n "${1:-}" ]] && SECRET_VALUES+=("$1")
  return 0
}

_literal_replace() {
  # Replace every literal occurrence of $2 in $1 with $3, assigning to REDACTED_TEXT.
  #
  # `${var//pattern/…}` treats the pattern as a glob, so the needle's metacharacters are escaped
  # first. Without this, a value containing `[` or `]` was never substituted — the claim in this
  # file's header said "everything in TAA_REDACT is removed" and it was not true.
  local text="$1" needle="$2" escaped
  if [[ -z "$needle" ]]; then
    REDACTED_TEXT="$text"
    return 0
  fi
  escaped="${needle//\\/\\\\}"
  escaped="${escaped//\*/\\*}"
  escaped="${escaped//\?/\\?}"
  escaped="${escaped//\[/\\[}"
  REDACTED_TEXT="${text//$escaped/$3}"
}

_redact_shapes() {
  # Credential shapes, in pure bash `=~` — no subprocesses. The first version piped the text
  # through four `sed` invocations per command, which made printing the plan take twenty seconds
  # under Git Bash on Windows (four process spawns x thirty commands): a redaction that makes the
  # tool unpleasant is a redaction somebody removes.
  local text="$1"
  while [[ "$text" =~ [0-9]{6,}:[A-Za-z0-9_-]{30,} ]]; do
    _literal_replace "$text" "${BASH_REMATCH[0]}" "$REDACT_PLACEHOLDER"
    text="$REDACTED_TEXT"
  done
  while [[ "$text" =~ [Bb]earer[[:space:]]+[A-Za-z0-9._-]{8,} ]]; do
    _literal_replace "$text" "${BASH_REMATCH[0]}" "$REDACT_PLACEHOLDER"
    text="$REDACTED_TEXT"
  done
  # A whole PEM block: header **and key material**. Redacting the header alone left the base64
  # body on the terminal.
  #
  # The three groups are contiguous on purpose, so `header + body` is an exact substring of the
  # text and the replacement always shortens it. The version before this one captured the body
  # *after* an unbracketed `[[:space:]]*`, so the needle skipped the newline between header and
  # body, matched nothing, and the `while` never terminated — an infinite loop in a script whose
  # output the operator is waiting for. The iteration bound below is the belt for that class of
  # bug: every branch here must strictly shrink `text`, and if one ever stops doing so, this
  # returns rather than hanging.
  # The whole block — header, body **and footer**. Leaving `-----END RSA PRIVATE KEY-----` behind
  # is harmless in itself, but the claim in this file's header is easier to keep honest when the
  # rule and the claim are the same shape. Each loop gets its own budget: sharing one meant that
  # past the bound the *second* loop still consumed it, so a text with a hundred blocks came out
  # with thirty-six bodies and a hundred footers untouched.
  local guard=0
  while [[ "$text" =~ (-----BEGIN\ [A-Z\ ]*PRIVATE\ KEY-----)([A-Za-z0-9+/=[:space:]]*)(-----END\ [A-Z\ ]*PRIVATE\ KEY-----) ]]; do
    (( guard++ < 256 )) || break
    _literal_replace "$text" \
      "${BASH_REMATCH[1]}${BASH_REMATCH[2]}${BASH_REMATCH[3]}" "$REDACT_PLACEHOLDER"
    text="$REDACTED_TEXT"
  done
  # A header with no footer (a truncated block): redact what is there.
  guard=0
  while [[ "$text" =~ -----BEGIN\ [A-Z\ ]*PRIVATE\ KEY----- ]]; do
    (( guard++ < 256 )) || break
    _literal_replace "$text" "${BASH_REMATCH[0]}" "$REDACT_PLACEHOLDER"
    text="$REDACTED_TEXT"
  done
  REDACTED_TEXT="$text"
}

redact_into() {
  # Assigning form: callers on a hot path use this and read `REDACTED_TEXT`.
  local text="${1:-}" value
  if [[ -n "${TAA_REDACT:-}" ]]; then
    for value in $TAA_REDACT; do
      _literal_replace "$text" "$value" "$REDACT_PLACEHOLDER"
      text="$REDACTED_TEXT"
    done
  fi
  for value in ${SECRET_VALUES[@]+"${SECRET_VALUES[@]}"}; do
    _literal_replace "$text" "$value" "$REDACT_PLACEHOLDER"
    text="$REDACTED_TEXT"
  done
  _redact_shapes "$text"
}

redact_text() {
  # Printing form: `redact_text "$x"` writes the safe text to stdout. What the tests drive.
  redact_into "${1:-}"
  printf '%s' "$REDACTED_TEXT"
}

render_into() {
  # A command line as it may be printed: **redacted first, then quoted**. The order is the point —
  # quoting turns a newline into `\n` and a space into `\ `, so redacting the quoted form would
  # miss exactly the values that most need it.
  local rendered="" quoted argument
  for argument in "$@"; do
    redact_into "$argument"
    printf -v quoted '%q' "$REDACTED_TEXT"
    rendered+="$quoted "
  done
  RENDERED_COMMAND="${rendered% }"
}

render_command() {
  render_into "$@"
  printf '%s' "$RENDERED_COMMAND"
}
