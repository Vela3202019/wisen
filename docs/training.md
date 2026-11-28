# Prompt or weights: training a local model on Wisen's signal

Wisen's Gardener puts a lesson in the prompt, as a rule in an instruction file.
The other place a lesson can live is the model's weights. This guide trains a
LoRA adapter on Apple silicon with [mlx-lm](https://github.com/ml-explore/mlx-lm),
serves it next to its base model, and compares the two with the paired replay
protocol, so both kinds of change are measured on the same cases with the same
judge and gate.

Local replay sends one chat completion per case. It measures the model's next
reply, not a full agent run with tools ([replay-eval.md](replay-eval.md#replay-on-a-local-model)).

## 1. Install

```bash
uv tool install 'mlx-lm==0.32.2'        # Apple silicon
```

As a rough guide, a 24 GB Mac fits LoRA on 1.5–4B models comfortably, and 7–8B
models in 4-bit with batch size 1–2 and `--grad-checkpoint`.

## 2. Make training data

Both commands write `train.jsonl` and `valid.jsonl` in mlx-lm's chat format,
plus `meta.json` (counts and, for private cases, IDs). Public distillation uses
Wisen's configured eval model as the teacher and needs its API key. A private
case export is local by default.

**Public: the demo rules.** The teacher writes fresh requests for each of the
six demo rules without receiving the evaluation prompts. Wisen filters out
requests that overlap with a demo case (word-set Jaccard ≥ 0.5), then the
teacher answers the rest while following the rules. The filter can miss
paraphrases: inspect generated requests before treating the results as evidence
of transfer. The examples carry no rule in the prompt.

```bash
wisen eval distill-demo --out data/demo            # 40 requests per rule + 40 clean
```

**Private: your case bank.** Freeze the manifest you will test on first, then
leave its cases out:

```bash
wisen eval export-sft --out ~/.wisen/training/mine --exclude manifest.json
```

The prompt is each case's replay prompt. For a correction, the target is the
agent's reply after your correction, read from the transcript. Review targets
and drop replies that still get the task wrong. **Optional `--rewrite` sends
the prompt, correction and replies to your configured eval provider** to write
a replacement target; use it only with data you may send there. A clean case's
target is the agent's original reply. Supply every frozen manifest with
`--exclude` (repeatable); Wisen skips their sessions and holdouts.
Only confirmed cases are used, unless you
pass `--include-mined`. The data contains your code: keep the data and the
adapter private.

With a few dozen examples, treat the run as a pipeline check, not a claim
about general model quality.

## 3. Train

```bash
export MODEL=mlx-community/Qwen2.5-Coder-3B-Instruct-4bit
sh scripts/train_mlx.sh data/demo adapters/demo   # from the Wisen checkout
```

The script defaults to 100 iterations, batch size 1 and a 1,024-token limit.
Set `ITERS=5` for a smoke test. `--mask-prompt` computes loss on replies only.
Sustained rising validation loss alongside falling training loss can indicate
overfitting; use held-out replay to see whether behavior actually improved.

## 4. Serve both models

```bash
mlx_lm.server --model $MODEL --port 8080                                # base
mlx_lm.server --model $MODEL --adapter-path adapters/demo --port 8081   # LoRA
```

The model name `default_model` makes each server answer with the model and
adapter it was started with. Name a server in the model string as
`local:NAME@URL`:

```bash
BASE=local:default_model@http://localhost:8080/v1
LORA=local:default_model@http://localhost:8081/v1
```

## 5. Compare

Each run is one paired comparison on the demo bank. Together, the four runs
cover the base and LoRA models, each with and without the rules:

| Question | Command |
|---|---|
| Do the rules help in the prompt? | `wisen eval demo --model $BASE --out prompt.json` |
| Do the weights learn them? | `wisen eval demo --model $BASE --proposed-model $LORA --rules none --out weights.json` |
| Do the weights match the prompt? | `wisen eval demo --model $BASE --proposed-model $LORA --rules current --out match.json` |
| Does the LoRA still add to the rules? | `wisen eval demo --model $BASE --proposed-model $LORA --rules both --out stack.json` |

`wisen eval report weights.json` prints each scorecard. Targeted cases show
what was learned. Holdout cases word the same rules differently, so they show
transfer rather than memorized phrasing. Clean cases show whether ordinary
replies regressed. For forgetting outside the demo, run a small public coding
benchmark against both servers; that is not built in. Use `--repeats 5` for a
result you publish; the judge runs on the eval model and costs API calls.

For your own cases, freeze one manifest per server (`wisen eval freeze …
--model $LORA`) and run `wisen eval paired --holdout` on each. The four-arm
comparison of models on frozen private cases is not built in yet.
