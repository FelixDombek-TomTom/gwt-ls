# gls-autolaunch — source this from the END of your interactive shell rc
#                  (.zshrc / .bashrc), after every other init is done.
#
# When `gls -r --new-tab` or `gls --restore-tabs` spawn a fresh shell,
# they set GLS_CLAUDE_RESUME_UUID=<uuid> in the env. This snippet picks
# it up, unsets it so it doesn't leak, and runs `claude -r <uuid>`
# inside your fully-loaded interactive shell — so claude inherits your
# real env (PATH, mise, tokens, aliases, everything). After claude
# exits you stay in a normal interactive shell.
#
# Without GLS_CLAUDE_RESUME_UUID set, this snippet is a no-op — the
# shell behaves like a normal new tab.
#
# POSIX-ish; works in bash, zsh, sh.

if [ -n "${GLS_CLAUDE_RESUME_UUID:-}" ]; then
  _gls_uuid="$GLS_CLAUDE_RESUME_UUID"
  unset GLS_CLAUDE_RESUME_UUID
  claude -r "$_gls_uuid"
  unset _gls_uuid
fi
