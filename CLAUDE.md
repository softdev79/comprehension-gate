# comprehension-gate

A merge gate for AI-assisted pull requests: measure the change, ask the owner code-specific
questions, grade the answers, require a signed sign-off. See README.md for the flow.

## Commands

- Tests: `python -m unittest discover -s tests` (no API key or network needed)
- Worker image: `docker build -f deploy/Dockerfile -t comprehension-gate-worker .`; deploy steps in `deploy/README.md`
- Evidence for a branch: `python comprehension_gate.py --repo <path> evidence --base origin/main`
- Offline questionnaire: add `generate --pr 1 --base origin/main --offline`
- Check the model endpoint: `python scripts/smoke_bedrock.py` (reads `.env`, see `.env.example`)

## Rules that must hold

- **The model never decides the gate.** `gate/agents.py` returns scores; only
  `gate/policy.py` turns them into pass/fail. Policy must stay deterministic and must
  reject missing, extra or out-of-range grades.
- **The diff and the answers are untrusted input.** The agents' only tools are the read-only
  ones in `gate/tools.py`: they read git objects at the base and head commits, never the
  working tree, network or environment, and nothing writes. Tool arguments are untrusted
  (paths confined to the repo, git run with an argument list, results size-capped, gate
  files hidden). Never add a tool that writes, runs commands or reaches the network. The
  agents finish with a submit tool whose output is schema- and semantically checked in
  `gate/agents.py`; `gate/agent_loop.py` bounds turns, tool calls and output size. Do not
  interpolate PR content into shell commands in the workflows (pass it through `env:`).
- **Measured facts come from `gate/evidence.py`, not from the model.** Versions, authorship
  and time are collected, then confirmed by the owner. Unknown stays "unknown", never 0%.
- **Nothing the gate trusts may be editable from the PR branch** without a check. Grades and
  status live in commit statuses and workflow artifacts, not in committed files. The
  questionnaire is committed, so its hash is published in a bot comment and verified.
- **The hook must never block or slow a session** and records no prompt text or file
  contents: paths, line counts and timestamps only. It always exits 0.
- **Never answer a questionnaire for the user.** The answers must be the owner's own.

## Conventions

- `gate/evidence.py`, `gate/policy.py`, `gate/questionnaire.py` and `hooks/` are standard
  library only and run on Python 3.9+. Only `gate/llm.py` imports a model SDK (`openai` or
  `anthropic`, inside functions so everything else works without them).
- Model calls go through `llm.complete_json`. Default provider is Amazon Bedrock's
  OpenAI-compatible endpoint with `openai.gpt-oss-120b` (`GATE_PROVIDER=openai`, settings in
  `.env.example`); `GATE_PROVIDER=anthropic` uses `claude-opus-5-5` with structured outputs.
  Every reply is validated against its schema in code; check the finish/stop reason first.
- `.env` is read only from this tool's folder, never from the gated repository.
- `service/` (GitHub App on Lambda) keeps trusted state in DynamoDB and reads
  `comprehension-gate.json` from the base branch. Webhook signatures are always verified, and
  events sent by the App's own bot are ignored so its commits cannot re-trigger the gate.
- Every new rule in `policy.py` gets a test in `tests/test_gate.py`.

## Next steps, in order

1. Run the three workflows on a test repository and fix what the YAML gets wrong.
2. Enforce the workflows from outside the PR branch (organization ruleset), and read
   `comprehension-gate.json` from the base branch instead of the PR head.
3. Ask only about changed lines when code is pushed after answers exist.
4. CODEOWNERS countersignature for high-risk changes.
5. Replace manifest parsing with a Syft SBOM; add tree-sitter/Semgrep facts to evidence.
6. Authorship from other tools (git notes / Agent Trace), and a dashboard of scores per repo.
