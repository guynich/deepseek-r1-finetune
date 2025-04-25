"""
Fine-tuning script for DeepSeek-R1-Distill-Llama-8B model on Apple Silicon.
This tutorial demonstrates how to fine-tune the model for medical reasoning tasks.
Optimized for M2/M3 Macs with ~36GB RAM.

References:
- Model: https://huggingface.co/deepseek-ai/DeepSeek-R1-Distill-Llama-8B
- Dataset: https://huggingface.co/datasets/FreedomIntelligence/medical-o1-reasoning-SFT
"""

import os
import torch
import platform
import warnings
import wandb
from datasets import load_dataset
from transformers import (
    AutoModelForCausalLM,
    AutoTokenizer,
    TrainingArguments,
    pipeline,
    logging,
    DataCollatorForLanguageModeling
)
from peft import LoraConfig, PeftModel
from trl import SFTTrainer

MAX_NEW_TOKENS = 2048
MODEL_NAME = "deepseek-ai/DeepSeek-R1-Distill-Qwen-1.5B"


# Suppress specific warnings
warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings("ignore", category=UserWarning)
logging.set_verbosity_error()

# Initialize wandb for experiment tracking
wandb.init(
    project="deepseek-r1-medical-finetuning",
    config={
        "model_name": MODEL_NAME,
        "learning_rate": 5e-5,
        "batch_size": 1,
        "num_epochs": 2,
        "hardware": "Apple Silicon",
        "dataset": "medical-o1-reasoning-SFT",
        "lora_rank": 8,
        "lora_alpha": 16
    }
)

# Initialize device for Apple Silicon
if platform.processor() == 'arm' and torch.backends.mps.is_available():
    print("Using Apple Silicon MPS (Metal Performance Shaders)")
    device = torch.device("mps")
elif torch.cuda.is_available():
    print("Using CUDA GPU")
    device = torch.device("cuda")
else:
    print("Using CPU")
    device = torch.device("cpu")

def prepare_dataset(tokenizer):
    """Prepare the medical reasoning dataset for training.

    Following DeepSeek's recommendation to include 'Please reason step by step'
    in the prompt for better reasoning performance.
    """
    dataset = load_dataset("FreedomIntelligence/medical-o1-reasoning-SFT", "en")
    print(f"Dataset loaded with {len(dataset['train'])} training examples")


    def format_instruction(sample):
        # TODO(guynich): <think> opening tag does not appear in response - these changes did not help.
        return f"""### Instruction:
Please reason step by step:

### Question:
{sample['Question']}

### Response:
<think>\n
{sample['Complex_CoT']}
</think>\n
{sample['Response']}
{tokenizer.eos_token}"""

    # For faster tutorial run change train_size to 0.05.
    dataset = dataset["train"].train_test_split(train_size=0.9, test_size=0.1, seed=42)

    # Prepare training dataset
    train_dataset = dataset["train"].map(
        lambda x: {"text": format_instruction(x)},
        remove_columns=dataset["train"].column_names,
        num_proc=os.cpu_count()
    )

    # Tokenize with optimized settings for speed
    train_dataset = train_dataset.map(
        lambda x: tokenizer(
            x["text"],
            truncation=True,
            padding="max_length",
            max_length=MAX_NEW_TOKENS,  # Reduce sequence length for faster training (for better results, use 2048)
            return_tensors=None,
        ),
        remove_columns=["text"],
        num_proc=os.cpu_count()
    )

    print(f"\nUsing {len(train_dataset)} examples for training")
    print("\nSample formatted data:")
    print(format_instruction(dataset["train"][0]))

    return train_dataset

def setup_model():
    """Setup the DeepSeek model with memory-efficient configuration for Apple Silicon."""
    model_name = MODEL_NAME

    # Set tokenizer configuration
    tokenizer = AutoTokenizer.from_pretrained(model_name, trust_remote_code=True)
    tokenizer.pad_token = tokenizer.eos_token
    tokenizer.padding_side = "right"

    # Load model with optimized settings for apple silicon
    model = AutoModelForCausalLM.from_pretrained(
        model_name,
        device_map="mps" if torch.backends.mps.is_available() else "auto",
        trust_remote_code=True,
        torch_dtype=torch.float16,
        use_cache=False,  # Disable KV-cache to save memory
        max_memory={0: "24GB"},  # Reserve memory for training
    )

    # Apply memory optimizations
    if hasattr(model, "enable_input_require_grads"):
        model.enable_input_require_grads()

    return model, tokenizer

def setup_trainer(model, tokenizer, train_dataset, eval_dataset):
    """Setup the LoRA training configuration following DeepSeek's recommendations."""
    # LoRA configuration optimized for quick learning
    peft_config = LoraConfig(
        lora_alpha=16,
        lora_dropout=0.1,
        r=4,  # Reduced rank for faster training
        bias="none",
        task_type="CAUSAL_LM",
        target_modules=["q_proj", "v_proj"]
    )

    # Training arguments optimized for speed and Apple Silicon
    training_args = TrainingArguments(
        output_dir="deepseek-r1-medical-finetuning",
        num_train_epochs=1,  # Single epoch for tutorial
        per_device_train_batch_size=2,
        gradient_accumulation_steps=4,
        learning_rate=1e-4,
        weight_decay=0.01,
        warmup_ratio=0.03,
        logging_steps=10,
        save_strategy="epoch",
        save_total_limit=1,
        fp16=True,  # Disable mixed precision for Apple Silicon
        bf16=False,
        optim="adamw_torch_fused",  # Use fused optimizer
        report_to="wandb",
        gradient_checkpointing=True,
        group_by_length=True,
        max_grad_norm=0.3,
        dataloader_num_workers=0,
        remove_unused_columns=True,
        run_name="deepseek-medical-tutorial",
        # Memory optimizations
        deepspeed=None,
        local_rank=-1,
        ddp_find_unused_parameters=None,
        torch_compile=False,
    )

    # Create data collator for proper padding
    data_collator = DataCollatorForLanguageModeling(
        tokenizer=tokenizer,
        mlm=False
    )

    # Initialize trainer with processing_class instead of tokenizer
    trainer = SFTTrainer(
        model=model,
        train_dataset=train_dataset,
        peft_config=peft_config,
        args=training_args,
        data_collator=data_collator,
        processing_class=None  # Let SFTTrainer handle processing
    )

    return trainer

def test_model(model_path):
    """Test the fine-tuned model following DeepSeek's usage recommendations."""
    # Create offload directory if it doesn't exist
    os.makedirs("offload", exist_ok=True)

    # Load model with proper memory management for Apple Silicon
    model = AutoModelForCausalLM.from_pretrained(
        model_path,
        device_map="auto",
        trust_remote_code=True,
        torch_dtype=torch.float16,
        offload_folder="offload",  # Specify offload directory
        offload_state_dict=True,   # Enable state dict offloading
        use_cache=False,           # Disable KV-cache to save memory
        max_memory={0: "24GB"},    # Limit memory usage
    )

    tokenizer = AutoTokenizer.from_pretrained(
        MODEL_NAME,
        trust_remote_code=True,
        padding_side="right"
    )

    # Initialize pipeline with optimized settings
    pipe = pipeline(
        "text-generation",
        model=model,
        tokenizer=tokenizer,
        device_map="auto",
        max_new_tokens=MAX_NEW_TOKENS,
        temperature=0.6,      # DeepSeek recommended temperature
        top_p=0.95,
        repetition_penalty=1.15,
        pad_token_id=tokenizer.eos_token_id
    )

    # Medical test case with recommended prompt format
    test_problem = """Please reason step by step:

A 45-year-old patient presents with sudden onset chest pain, shortness of breath, and anxiety. The pain is described as sharp and worsens with deep breathing. What is the most likely diagnosis and what immediate tests should be ordered?"""

    try:
        result = pipe(
            test_problem,
            max_new_tokens=MAX_NEW_TOKENS,
            temperature=0.6,
            top_p=0.95,
            repetition_penalty=1.15
        )

        print("\nTest Problem:", test_problem)
        print("\nModel Response:", result[0]["generated_text"])

        # Log test results to wandb
        wandb.log({
            "test_example": wandb.Table(
                columns=["Test Case", "Model Response"],
                data=[[test_problem, result[0]["generated_text"]]]
            )
        })
    except Exception as e:
        print(f"\nError during testing: {str(e)}")
        print("Model was saved successfully but testing failed. You can load the model separately for testing.")
    finally:
        # Clean up
        if os.path.exists("offload"):
            import shutil
            shutil.rmtree("offload")

def main():
    """Main function to run the fine-tuning process."""
    try:
        print("\nSetting up model...")
        model, tokenizer = setup_model()

        print("\nPreparing dataset...")
        train_dataset = prepare_dataset(tokenizer)

        print("\nSetting up trainer...")
        trainer = setup_trainer(model, tokenizer, train_dataset, None)

        print("\nStarting training...")
        trainer.train()

        print("\nSaving model...")
        trainer.model.save_pretrained("./fine_tuned_model")

        print("\nTesting model...")
        test_model("./fine_tuned_model")

    finally:
        wandb.finish()

if __name__ == "__main__":
    main()
