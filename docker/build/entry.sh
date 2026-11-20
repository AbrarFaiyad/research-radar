#!/bin/sh
# Runs one Claude Code build inside the sandbox. MODEL, TURNS and BUILD_TIMEOUT_S come from the
# host via --env. The time limit holds even if the host process that started docker is killed.
# The prompt goes in on stdin so a leading "-" can never be read as an option.
# The log goes to /work/.logs (outside .task, so the trap below keeps it); the host moves it out.
set -u
mkdir -p "$HOME" /work/.logs
trap 'rm -rf /work/.task' EXIT
timeout --kill-after=30 "$BUILD_TIMEOUT_S" claude -p \
    --model "$MODEL" \
    --max-turns "$TURNS" \
    --permission-mode bypassPermissions \
    --output-format text < /work/.task/prompt.md > /work/.logs/claude.log 2>&1
