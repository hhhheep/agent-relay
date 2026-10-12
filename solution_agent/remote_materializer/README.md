# Remote Materializer

`remote_materializer` is the low-cost worker path for task books that only need material generation and validation.

It is designed to run either locally or on the remote host:

```powershell
python -m solution_agent.remote_materializer --task path\to\task.md --output-root runs --reader-output reader.json --materializer-output qwen.md --print-json
```

Current implementation uses injectable model clients. The CLI accepts precomputed raw outputs so smoke tests and remote dry runs do not require API keys.

## Install

The minimal runtime uses the existing project dependencies:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

No provider SDK is required for the first live Bailian path. The client uses the OpenAI-compatible HTTP endpoint through Python's standard library.

## Provider Policy

Default live policy:

```text
solution_agent/remote_materializer/config/provider_policy.bailian.yaml
```

It defines:

```text
primary_reader: qwen-flash
primary_materializer: qwen3-coder-next
patch_materializer: qwen3-coder-30b-a3b-instruct
validator: local_python
fallback: codex
```

Hermes + Gemini remains advisory only and is not called by this first live main path.

## API Keys

Copy the env template to the remote controller:

```bash
mkdir -p ~/.remote_materializer
cp solution_agent/remote_materializer/config/remote_materializer.env.example ~/.remote_materializer/.env
chmod 600 ~/.remote_materializer/.env
```

Edit:

```bash
DASHSCOPE_API_KEY="..."
```

## Dry Run

```bash
python -m solution_agent.remote_materializer \
  --task solution_agent/remote_materializer/examples/probe_inference_task.md \
  --output-root runs \
  --reader-output solution_agent/remote_materializer/examples/work_order.probe_inference.example.json \
  --materializer-output path/to/qwen_materializer_output.example.md \
  --print-json
```

## Live Bailian Run

```bash
python -m solution_agent.remote_materializer \
  --task path/to/task.md \
  --output-root runs \
  --live \
  --env-file ~/.remote_materializer/.env \
  --provider-policy solution_agent/remote_materializer/config/provider_policy.bailian.yaml \
  --print-json
```

Live mode performs:

```text
task.md -> qwen-flash reader -> work_order.json
work_order.json -> context_pack -> qwen3-coder materializer -> generated artifacts
generated artifacts -> local validators -> result_package.yaml
```

It does not run training, run inference, mutate the scheduler registry, or apply patches.

