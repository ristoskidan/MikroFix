#!/bin/sh
set -eu

state_dir="/home/mikrofix/.local/state/mikrofix"
log_file="$state_dir/agent-console.log"
viewer_lock="$state_dir/agent-console-viewer.lock"

mkdir -p "$state_dir"
touch "$log_file"

exec 9>"$viewer_lock"
if ! flock -n 9; then
    printf '%s\n' 'Spremljevalnik lokalnih agentov je ze odprt.'
    sleep 2
    exit 0
fi

clear
printf '%s\n' \
    'MIKROFIX — LOKALNI POMOCNIKI' \
    'To okno ostane odprto in prikazuje vse nove zahteve ter odgovore.' \
    'Naloge se izvajajo zaporedno, zato se odgovori ne premesajo.' \
    'Okno lahko zapres; dnevnik se s tem ne izbrise.' \
    '========================================================================'

exec tail -n 200 -F "$log_file"
