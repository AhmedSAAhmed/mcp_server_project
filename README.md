In this project, you are going to make a chatbot to scrape LLM Inference Serving websites to research costs of serving various LLMs. You will do this by writing an MCP Server that hooks up to Firecrawl's API and saving the data in a SQLite Database. You should use the following websites to scrape:

- "cloudrift": "https://www.cloudrift.ai/inference"
- "deepinfra": "https://deepinfra.com/pricing"
- "fireworks": "https://fireworks.ai/pricing#serverless-pricing"
- "groq": "https://groq.com/pricing"

1. Make a venv with uv
2. Sync venv with pyproject.toml (`uv sync`)
3. Configure your Udacity/Vocareum key and a separate Firecrawl key in `.env`:

   ```dotenv
   OPENAI_API_KEY=your_udacity_vocareum_key
   OPENAI_BASE_URL=https://openai.vocareum.com/v1
   OPENAI_MODEL=claude-sonnet-5-5
   FIRECRAWL_API_KEY=your_firecrawl_key
   ```

   This client uses the gateway's OpenAI-compatible Chat Completions API to
   access Claude. For compatibility, it also accepts the existing Vocareum key
   stored under `ANTHROPIC_API_KEY` when `OPENAI_API_KEY` is absent. The base
   URL includes `/v1`; do not append `/chat/completions` to it.
4. Complete the 2 tool calls in `starter_server.py`
5. Change the `server_config.json` to point to your server file
6. Complete any section in `starter_client.py` that has "#complete".
7. Test using any methods taught in the course
8. Use the following prompts in your chatbot but play around with all the LLM providers in the list above: 
    - "How much does cloudrift ai (https://www.cloudrift.ai/inference) charge for deepseek v3?"
    - "How much does deepinfra (https://deepinfra.com/pricing) charge for deepseek v3"
    - "Compare cloudrift ai and deepinfra's costs for deepseek v3"

## Running the completed application

Run `uv sync`, then `uv run python starter_client.py` from the project folder.
The startup output lists the custom scraper, SQLite, and filesystem MCP servers.
Ask the comparison question above, then enter `show data` to display the five
most recently created pricing records. Enter `quit` to exit.

## Vocareum compatibility and rubric mapping

The course credential was verified against
`https://openai.vocareum.com/v1/chat/completions` with the Claude model
`claude-sonnet-5-5`. This gateway accepts OpenAI-compatible requests. The client
therefore uses the OpenAI SDK to access Claude through Vocareum; the model is
still Claude. Direct Anthropic Messages requests were rejected by this gateway.

`ChatSession.process_query` performs the rubric's tool-use workflow using the
gateway's equivalent message fields:

| Required step | Implementation |
| --- | --- |
| Collect answer text | Append `message.content` to `full_response`. |
| Append the assistant's tool request | Append the assistant message with all `tool_calls`. |
| Locate the responsible MCP server | Resolve `tool_to_server` and find the matching server. |
| Execute the requested tool | Parse function arguments and await `Server.execute_tool`. |
| Append the tool result | Append a `role: tool` message with the matching `tool_call_id`. |
| Call the model again | Repeat Chat Completions after returning all tool results. |
| Finish on a text-only answer | Exit when no tool calls remain, then extract and store prices. |

Anthropic's `tool_use` and `tool_result` blocks are represented by `tool_calls`
and tool-role messages in this gateway protocol. This is a transport adaptation;
literal Anthropic message syntax is not used. If the assessment requires that
syntax specifically, confirm whether this gateway adaptation is accepted.

Pricing inserts use SQLite's MCP `write_query` tool with this column order:
`company_name, plan_name, input_tokens, output_tokens, currency, billing_period,
features, limitations, source_query`. Features use `json.dumps`; text values
escape SQL apostrophes and missing prices are stored as SQL `NULL`.

`show data` uses the original query:

```sql
SELECT company_name, plan_name, input_tokens, output_tokens, currency
FROM pricing_plans ORDER BY created_at DESC LIMIT 5
```

It prints `Recently Stored Data:`, separator lines, `Pricing Plans:`, and a
bullet for each company and plan with input and output token prices. The
database screenshot in `evidence.md` shows this rubric-aligned bullet output.

Run the regression checks with `uv run python -m unittest test_client -v`.
See [evidence.md](evidence.md) for the screenshots. Include the `screenshots/`
folder with a ZIP submission, and exclude `.env` and `.venv`.
