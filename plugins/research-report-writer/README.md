# Research Report Writer

MCP tools for producing evidence-grounded Markdown with a fixed
Alibaba Cloud Model Studio (Bailian) DeepSeek V4-Flash-0731 profile. The model ID
is `deepseek-v4-flash-0731`. Reports can be written locally or in place on an SSH
workplace.

The tool server never accepts an API key as a tool argument. It reads provider
credentials from environment variables and returns only safe configuration
metadata. Generated reports are written to disk; tool results contain paths,
hashes, usage totals, and claim counts rather than the full Markdown.

## Tools

- `provider_status`: report secret-safe provider configuration status.
- `validate_evidence_packet`: validate a compact JSON evidence packet without a network call.
- `draft_research_report`: call the configured provider and write a Markdown report plus provenance sidecar.
- `remote_writer_status`: inspect secret-safe SSH writer configuration.
- `validate_remote_evidence_packet`: validate evidence that already lives on the remote workplace.
- `draft_remote_research_report`: write Markdown in place remotely without transferring the evidence or report.

Codex performs the final review against the evidence packet; no second API model is used for auditing.

## Remote credential transport

The remote tools do not copy evidence or Markdown to the local machine. At call
time the MCP server reads the configured provider key from the local process and
sends it with the request envelope over encrypted SSH stdin. The key is never a
tool argument, command-line argument, shell export, history entry, or remote
file. The remote Python process uses it for one API call and exits.

The current workplace defaults are `EXAMPLE_WORKER_A` and
`/srv/example-workspaces/fan-workspace`. The remote helper uses only Python's
standard library; Node and pnpm are not required on the worker.

## Provider environment

Set both `DASHSCOPE_API_KEY` and `BAILIAN_BASE_URL`, or point
`REPORT_WRITER_ENV_FILE` at a bounded local env file containing them. The
bundled personal configuration reuses
`C:\Users\example-user\.remote_materializer\.env`, the same secret source as
solution-agent; key values are never returned by status tools.

`BAILIAN_BASE_URL` may be an official regional shared endpoint, for example:

```text
https://dashscope.aliyuncs.com/compatible-mode/v1
https://dashscope-intl.aliyuncs.com/compatible-mode/v1
```

For production, prefer the workspace-specific OpenAI-compatible URL copied
from Model Studio, for example:

```text
https://{WorkspaceId}.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1
```

China (Beijing), Singapore, US (Virginia), Germany, and Japan use different
region hostnames. The plugin accepts only official shared hosts or
credential-free HTTPS workspace hosts under `.maas.aliyuncs.com`, all ending
in `/compatible-mode/v1`. The model is fixed to
`deepseek-v4-flash-0731`, structured JSON output is requested, thinking is
explicitly disabled, and output is capped at 8192 tokens.

`REPORT_WRITER_SSH_CONFIG` may point to the existing solution-agent SSH config
so aliases such as `EXAMPLE_WORKER_A` work without copying SSH settings.

Set `REPORT_WRITER_ALLOWED_ROOTS` to one or more absolute paths separated by the
platform path delimiter. All input and output files must remain within those
roots.

## Evidence packet

See `examples/evidence.example.json`. Claims are intentionally explicit and
carry stable IDs and source paths. The writer is instructed to cite those IDs,
separate observations from interpretations, and retain limitations.

## Development

Use pnpm's hoisted node linker when preparing the personal plugin. This gives
Codex's plugin cache portable package directories instead of Windows symlinks
that may be omitted during cache copying.

```powershell
pnpm install --frozen-lockfile --config.node-linker=hoisted
pnpm test
pnpm start
```

Tests are offline and use a fake provider transport. A live API smoke test is
not performed unless a provider key and model have been configured explicitly.
