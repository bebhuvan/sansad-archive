# OpenRouter APIs — Space Bunny Alpha (stealth/space-bunny-alpha)

Guide for calling every confirmed OpenRouter API that serves `stealth/space-bunny-alpha`.

Model page: https://openrouter.ai/stealth/space-bunny-alpha
Create an API key: https://openrouter.ai/settings/keys

## Authentication

Send this header with every request:

- Authorization: Bearer $OPENROUTER_API_KEY

## Text / Chat Completions API

Generate text with `stealth/space-bunny-alpha` through OpenRouter's Chat Completions API.

Docs: https://openrouter.ai/docs/api/api-reference/chat/create-a-chat-completion

### Endpoint

POST https://openrouter.ai/api/v1/chat/completions

Headers:
- Content-Type: application/json

### Request fields (stealth/space-bunny-alpha)

- model: string (required) — `"stealth/space-bunny-alpha"`
- messages: array (required) — ordered conversation messages with `role` and `content`
- stream: boolean (optional) — return Server-Sent Events as tokens are generated
- max_tokens: optional — accepted by this model; see the API reference for its value shape
- reasoning: optional — accepted by this model; see the API reference for its value shape
- reasoning_effort: optional — accepted by this model; see the API reference for its value shape
- response_format: optional — accepted by this model; see the API reference for its value shape
- temperature: optional — accepted by this model; see the API reference for its value shape
- tool_choice: optional — accepted by this model; see the API reference for its value shape
- tools: optional — accepted by this model; see the API reference for its value shape
- top_p: optional — accepted by this model; see the API reference for its value shape

The model-specific optional fields above come from this model's advertised capabilities.
`stream` and `provider` routing preferences are API controls accepted independently of that
model capability list.

### Response

Without `stream`, the response is a Chat Completions JSON object:

```json
{
  "id": "gen-abc123",
  "choices": [{ "message": { "role": "assistant", "content": "..." } }],
  "usage": { "prompt_tokens": 12, "completion_tokens": 24, "total_tokens": 36, "cost": 0.001 }
}
```

With `stream: true`, the response is `text/event-stream`: read each `data:` JSON chunk until
`data: [DONE]`. The final usage chunk carries token counts and cost when usage is requested.

### Examples

#### Chat completion

```bash
curl -X POST https://openrouter.ai/api/v1/chat/completions \
  -H "Authorization: Bearer $OPENROUTER_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
  "model": "stealth/space-bunny-alpha",
  "messages": [
    {
      "role": "user",
      "content": "What is the meaning of life?"
    }
  ]
}'
```

### Error differences

- 400 — malformed messages or a parameter this model does not support
- 502 — the upstream text generation failed

## Errors

Failures return `{"error": {"code": <number>, "message": <string>}}` with the HTTP status:

- 400 — malformed request or an unsupported parameter
- 401 — missing or invalid API key
- 402 — insufficient credits
- 403 — spend limit reached, key disabled, or access blocked
- 404 — unknown model or no provider can serve the request
- 429 — rate limited; retry with backoff
- 502 — the operation failed upstream; failed generations are not billed

---

Canonical version of this document: https://openrouter.ai/stealth/space-bunny-alpha/llms.txt
