"""Train a reply-only LoRA on Wisen chat JSONL. Run in the MLX environment."""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path


def check_data(data: Path, process, limit: int) -> None:
    for split in ("train", "valid"):
        path = data / f"{split}.jsonl"
        count = 0
        with path.open(encoding="utf-8") as rows:
            for count, line in enumerate(rows, start=1):
                tokens, offset = process(json.loads(line))
                if offset >= min(limit, len(tokens)):
                    raise ValueError(f"{path.name}:{count}: no reply tokens fit; shorten the prompt or "
                                     "increase --max-seq-length before training")
        if not count:
            raise ValueError(f"{path.name} is empty")


def train(data: Path, adapter: Path, model: str, iters: int, limit: int) -> None:
    if adapter.exists():
        raise ValueError("Use a new adapter directory to preserve the previous run")
    from mlx_lm.tuner.datasets import ChatDataset
    from mlx_lm.utils import load_tokenizer

    dataset = ChatDataset([], load_tokenizer(model), mask_prompt=True)
    check_data(data, dataset.process, limit)
    subprocess.run([sys.executable, "-m", "mlx_lm", "lora", "--model", model, "--train",
                    "--data", str(data), "--adapter-path", str(adapter), "--mask-prompt", "--grad-checkpoint",
                    "--batch-size", "1", "--max-seq-length", str(limit), "--num-layers", "8",
                    "--iters", str(iters), "--seed", "42"], check=True, umask=0o077)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("data", type=Path)
    parser.add_argument("adapter", type=Path)
    parser.add_argument("--model", default=os.environ.get("MODEL", "mlx-community/Qwen2.5-Coder-3B-Instruct-4bit"))
    parser.add_argument("--iters", type=int, default=100)
    parser.add_argument("--max-seq-length", type=int, default=1024)
    args = parser.parse_args()
    if args.iters < 1 or args.max_seq_length < 2:
        parser.error("--iters must be positive and --max-seq-length must be at least 2")
    try:
        train(args.data, args.adapter, args.model, args.iters, args.max_seq_length)
    except (ValueError, OSError) as exc:
        parser.exit(1, f"{exc}\n")
