#!/usr/bin/env bash
set -uo pipefail
input="$(cat)"
active="$(printf '%s' "$input" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("stop_hook_active", False))' 2>/dev/null)"
[[ "$active" == "True" ]] && exit 0
root="$(printf '%s' "$input" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("cwd", ""))' 2>/dev/null)"
[[ -d "$root" ]] || exit 0
left=()
for repo in "$root"/*/; do
  [[ -e "$repo/.git" ]] || continue
  name="$(basename "$repo")"
  if [[ -n "$(git -C "$repo" status --porcelain --untracked-files=no 2>/dev/null)" ]]; then
    left+=("$name has uncommitted changes")
  elif [[ -n "$(git -C "$repo" rev-list HEAD --not --remotes 2>/dev/null | head -1)" ]]; then
    left+=("$name has commits that are not pushed")
  fi
done
(( ${#left[@]} == 0 )) && exit 0
printf 'Not finished: %s. Commit, push and open the pull request, or say in your reply why the work stays local. Never commit scratch files, screenshots or env files.\n' "$(IFS=';'; echo "${left[*]}")" >&2
exit 2
