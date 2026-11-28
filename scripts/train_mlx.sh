#!/bin/sh
set -eu

if [ "$#" -ne 2 ]; then
    echo "Usage: $0 DATA_DIRECTORY NEW_ADAPTER_DIRECTORY" >&2
    exit 2
fi
for split in train valid; do
    if [ ! -s "$1/$split.jsonl" ]; then
        echo "Missing or empty $split.jsonl in the data directory" >&2
        exit 1
    fi
done
if [ -e "$2" ]; then
    echo "Use a new adapter directory to preserve the previous run" >&2
    exit 1
fi

umask 077
exec mlx_lm.lora --model "${MODEL:-mlx-community/Qwen2.5-Coder-3B-Instruct-4bit}" \
    --train --data "$1" --adapter-path "$2" --mask-prompt --grad-checkpoint \
    --batch-size 1 --max-seq-length 1024 --num-layers 8 --iters "${ITERS:-100}" --seed 42
