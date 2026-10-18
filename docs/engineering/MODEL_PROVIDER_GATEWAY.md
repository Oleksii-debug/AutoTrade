# Model provider gateway scope

AutoTrade defaults to `ZERO`/local routing. Remote inference remains explicitly
opt-in through the existing durable `ModelCallBinding`, exact budget reservation,
pricing evidence and model-call journal. A model response has no tools, order,
risk or trading authority.

The dependency-free OpenAI-compatible adapter has a closed source-owned endpoint
registry for Groq, Cerebras, DeepSeek, OpenRouter, NVIDIA NIM and SambaNova.
Hosts and paths are not configurable. Calls are non-streaming and bounded by
input bytes, response bytes, output tokens, timeout and a USD reservation ceiling.
Provider keys are stored per provider/profile in Windows Credential Manager and
are never accepted on a command line.

Gemini and generic Hugging Face inference are deliberately excluded from this
adapter. Their native protocols, endpoint selection and usage accounting are not
one fixed OpenAI chat-completions contract. A future adapter must define those
contracts independently rather than weakening the closed registry.

No automatic retries or implicit provider fallback are performed: an ambiguous
remote outcome must flow through the durable model-call recovery and billing
reconciliation authority.

Endpoint references (reviewed 2026-10-18):

- [Groq API reference](https://console.groq.com/docs/api-reference)
- [Cerebras OpenAI migration example](https://inference-docs.cerebras.ai/resources/glm-47-migration)
- [DeepSeek Chat Completions](https://api-docs.deepseek.com/api/create-chat-completion/)
- [OpenRouter Chat Completions](https://openrouter.ai/docs/api/api-reference/chat/send-chat-completion-request)
- [NVIDIA hosted NIM endpoint](https://docs.nvidia.com/rag/latest/vlm.html)
- [SambaNova API quickstart](https://cloud.sambanova.ai/)
