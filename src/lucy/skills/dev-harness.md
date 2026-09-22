# Dev Harness Setup
The dev harness provides a structured environment for agent interaction and development.

## Key Components
- **Inference Engine**: Powered by `llama.cpp` using the Vulkan backend.
- **Core Model**: Lux-Plus-M12B.
- **Port**: 8085.
- **Context & Efficiency**:
  - Context Size: 100,000 tokens.
  - Flash Attention: Enabled.
  - Cache Types: Q8_0 (K and V).
- **Memory System**: SQLite-based storage located at `.core/memories/lucy/sessions.sqlite`, supporting session-based persistence and automatic summarization.
