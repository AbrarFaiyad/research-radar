#!/bin/sh
# Runs one Claude Code build inside the sandbox. MODEL and TURNS come from the host via --env.
set -u
mkdir -p "$HOME"
trap 'rm -rf /work/.task' EXIT
claude -p "$(cat /work/.task/prompt.md)" \
    --model "$MODEL" \
    --max-turns "$TURNS" \
    --permission-mode bypassPermissions \
    --output-format text > /work/.task/claude.log
