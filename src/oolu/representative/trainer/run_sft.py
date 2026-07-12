"""The pinned QLoRA SFT script the subprocess trainer runs by default.

Reads an SftConfig JSON, trains a PEFT LoRA adapter on the chat-format
JSONL corpus, evaluates holdout perplexity, and writes the adapter plus
``metrics.json`` into the config's output_dir — the exact contract
SubprocessTrainer checks. Retraining is always FROM BASE on the current
corpus (never continual updates), which is what keeps refreshes drift-free.

Needs the training extra on a CUDA box:

    pip install 'oolu[representative-train]'
    python -m oolu.representative.trainer.run_sft --config sft-config.json
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

from .sft import METRICS_FILE, SftConfig

try:  # The heavy stack lives behind the extra; fail with the fix, not a trace.
    import torch
    from datasets import Dataset
    from peft import LoraConfig
    from transformers import AutoModelForCausalLM, AutoTokenizer
    from trl import SFTConfig, SFTTrainer
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
    parser.add_argument("--config", required=True, help="path to SftConfig JSON")
    config = SftConfig.load(parser.parse_args().config)
    output_dir = Path(config.output_dir)

    tokenizer = AutoTokenizer.from_pretrained(config.base_model)
    model = AutoModelForCausalLM.from_pretrained(
        config.base_model,
        torch_dtype=torch.bfloat16,
        device_map="auto",
    )

    def rows(path: str) -> Dataset:
        # Per-sample weights ride as repetition-free loss weights where the
        # trainer supports them; the messages column is what SFTTrainer
        # templates. One-liner downweighting is already in the corpus file.
        return Dataset.from_list(
            [{"messages": example["messages"]} for example in _load_jsonl(path)]
        )

    trainer = SFTTrainer(
        model=model,
        processing_class=tokenizer,
        train_dataset=rows(config.train_path),
        eval_dataset=rows(config.holdout_path),
        peft_config=LoraConfig(
            r=config.rank,
            lora_alpha=config.rank * 2,
            lora_dropout=0.05,
            bias="none",
            task_type="CAUSAL_LM",
            # Attention-only targets: the safe default across dense AND MoE
            # bases (expert MLPs stay frozen), and plenty for voice.
            target_modules=["q_proj", "k_proj", "v_proj", "o_proj"],
        ),
        args=SFTConfig(
            output_dir=str(output_dir / "checkpoints"),
            num_train_epochs=config.epochs,
            learning_rate=config.learning_rate,
            max_length=config.max_seq_len,
            per_device_train_batch_size=4,
            gradient_accumulation_steps=4,
            logging_steps=20,
            save_strategy="no",
            report_to=[],
            bf16=True,
        ),
    )
    trainer.train()
    eval_loss = trainer.evaluate().get("eval_loss")
    trainer.save_model(str(output_dir))

    (output_dir / METRICS_FILE).write_text(
        json.dumps(
            {"holdout_ppl": math.exp(eval_loss) if eval_loss is not None else None}
        ),
        encoding="utf-8",
    )


if __name__ == "__main__":  # pragma: no cover
    main()
