"""The pinned DPO script the subprocess preference trainer runs by default.

Reads a DpoConfig JSON, loads the base model with the freshly trained SFT
adapter on top, runs a short DPO pass over the user's (prompt, chosen,
rejected) edit pairs, and saves the tuned adapter into output_dir — the
same contract run_sft.py honors, checked by the same code.

Needs the training extra on a CUDA box:

    pip install 'oolu[representative-train]'
    python -m oolu.representative.trainer.run_dpo --config dpo-config.json
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .dpo import DpoConfig
from .sft import METRICS_FILE

try:  # The heavy stack lives behind the extra; fail with the fix, not a trace.
    import torch
    from datasets import Dataset
    from peft import PeftModel
    from transformers import AutoModelForCausalLM, AutoTokenizer
    from trl import DPOConfig as TrlDpoConfig
    from trl import DPOTrainer
except ImportError as exc:  # pragma: no cover - exercised only without the extra
    raise SystemExit(
        "training needs the representative-train extra:"
        " pip install 'oolu[representative-train]'"
        f" (missing: {exc.name})"
    ) from exc


def _load_jsonl(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def main() -> None:  # pragma: no cover - needs a GPU and the extra
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, help="path to DpoConfig JSON")
    config = DpoConfig.load(parser.parse_args().config)
    output_dir = Path(config.output_dir)

    tokenizer = AutoTokenizer.from_pretrained(config.base_model)
    base = AutoModelForCausalLM.from_pretrained(
        config.base_model,
        torch_dtype=torch.bfloat16,
        device_map="auto",
    )
    # The SFT adapter is the starting point AND the trainable module;
    # DPOTrainer derives its frozen reference from the same weights.
    model = PeftModel.from_pretrained(base, config.adapter_dir, is_trainable=True)

    trainer = DPOTrainer(
        model=model,
        processing_class=tokenizer,
        train_dataset=Dataset.from_list(_load_jsonl(config.pairs_path)),
        args=TrlDpoConfig(
            output_dir=str(output_dir / "checkpoints"),
            num_train_epochs=config.epochs,
            learning_rate=config.learning_rate,
            beta=config.beta,
            max_length=config.max_seq_len,
            per_device_train_batch_size=2,
            gradient_accumulation_steps=8,
            logging_steps=10,
            save_strategy="no",
            report_to=[],
            bf16=True,
        ),
    )
    trainer.train()
    trainer.save_model(str(output_dir))

    # DPO has no perplexity story; the SFT stage's ppl stays the registry
    # metric. An empty metrics file keeps the output contract explicit.
    (output_dir / METRICS_FILE).write_text(
        json.dumps({"holdout_ppl": None}), encoding="utf-8"
    )


if __name__ == "__main__":  # pragma: no cover
    main()
