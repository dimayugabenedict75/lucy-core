---
skill_id: hf-download
name: Hugging Face Download
description: Download models, datasets, and files from Hugging Face Hub using the hf CLI
trigger: huggingface download hf model dataset
tools:
  - hf_download
category: ml
---

# Hugging Face Download

Use the `hf_download` tool when the user asks to download models, datasets, or individual files from Hugging Face Hub.

## Usage Patterns

- `hf_download(repo_id="NousResearch/Llama-2-7b-chat-hf", filename="README.md")` — download a specific file
- `hf_download(repo_id="TheBloke/Llama-2-7B-Chat-GGUF", filename="llama-2-7b-chat.Q4_K_M.gguf")` — download a GGUF model file
- `hf_download(repo_id="username/my-dataset", repo_type="dataset")` — download a dataset repo

## Notes

- Files save to `C:/Users/dimay/Downloads/hf_cache/<repo_id>/`
- Uses the `hf` CLI (modern huggingface_hub)
- Unauthenticated downloads have lower rate limits
- Large files may take time — timeout is 120s
