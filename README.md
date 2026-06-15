# Hermes Swarm

A self-hostable **multi-agent swarm server** with a real-time dashboard. Each
agent is a full [Hermes](https://github.com/NousResearch/hermes-agent) agent —
it can browse the web, run a terminal, write files, and publish to live
platforms — and agents collaborate peer-to-peer on a shared project. You watch
and steer the whole team from one web UI.

- **Live execution view** — see each agent think → call tools → answer, in real time.
- **Per-agent config** — model, provider, tools, reasoning, iterations, sampling — from the UI.
- **Self-aware agents** — agents can read their own config/telemetry and *propose* changes for your approval.
- **Human inbox** — agents ask you for logins/decisions; you reply from the dashboard.

> **New here?** The **[Getting Started guide](docs/getting-started.md)** walks
> you from download through building and running your first team. Exposing it on
> a server? See **[Deploying on a VPS](docs/deploy-vps.md)**.

---

## Prerequisites

1. **An LLM provider API key.** You configure it with **`hermes setup`** — Hermes'
   own one-command wizard that lets you pick from 40+ providers (Anthropic,
   OpenAI, OpenRouter, Groq, DeepSeek, a local model, …), paste your key, and
   choose a model. The swarm reads that config directly; there's nothing
   provider-specific to set up separately. *(Already point at a
   [LiteLLM](https://github.com/BerriAI/litellm)/OpenAI-compatible proxy? You can
   instead set `SWARM_LLM_*` — see [Configuration](#configuration-environment-variables).)*
2. **Docker** *(option A)* **or Python 3.11+** *(option B)*.

You do **not** need to install Hermes separately — it comes in automatically,
and the `hermes` CLI ships with it (see [Already have Hermes?](#already-have-hermes)).

---

## Option A — Docker (recommended)

```bash
git clone <this-repo> hermes-swarm && cd hermes-swarm
cp .env.example .env          # optional — only for the proxy path (see below)
docker compose up --build
```

Open **http://127.0.0.1:8000**. The image bundles Python, Hermes, Chromium, and
the dashboard; your data persists in the `swarm-data` volume.

**Configure your provider** with Hermes' own wizard, written to the swarm's
shared config on the volume (so it survives restarts and becomes the default):

```bash
docker compose run --rm -e HERMES_HOME=/data/.hermes-shared swarm hermes setup
```

> Prefer an OpenAI-compatible / LiteLLM proxy (e.g. one running on your **host**)?
> Skip `hermes setup` and instead set `SWARM_LLM_BASE_URL` (e.g.
> `http://host.docker.internal:4000/v1`) + `SWARM_LLM_API_KEY` + `SWARM_DEFAULT_MODEL`
> in `.env`.

---

## Option B — pip

```bash
python3 -m venv .venv && source .venv/bin/activate   # Python 3.11+
pip install .                       # pulls hermes-agent (+ the `hermes` CLI) + deps
playwright install chromium         # for the browser-publishing tools

hermes setup                        # pick provider + key + model (saved in ~/.hermes)

hermes-swarm doctor                 # confirm Hermes + your model + Chromium
hermes-swarm init                   # scaffold a starter team + coordinator agent
hermes-swarm up                     # serve the dashboard on http://127.0.0.1:8000
```

That's it — `hermes setup` is the only place you enter a provider or key. The
swarm picks up whatever you configured there.

---

## Already have Hermes?

If you already use Hermes (`~/.hermes/`), there's nothing extra to do:

- **Runtime** — the swarm reuses your installed agent runtime. Resolution order:
  the pip-installed `hermes-agent`, else `HERMES_AGENT_PATH`, else
  `~/.hermes/hermes-agent`.
- **Provider/model** — if you've already run `hermes setup`, the swarm **adopts
  that config automatically** (you don't run it again). It *reads* your provider
  + model + key from `~/.hermes`; it never writes there.
- **Secrets** — all of your `~/.hermes/.env` keys are imported into the swarm at
  startup (not just the model provider's key) — so tool keys like
  `FIRECRAWL_API_KEY`, `TAVILY_API_KEY`, `EXA_API_KEY`, etc. are available to
  agents too. Import is non-overriding: explicit server/deployment env wins.
- **Isolation** — your personal `~/.hermes` is left untouched. Each agent runs in
  its own private Hermes home under `<data>/teams/…`, so swarm agents don't share
  (or pollute) your memory, sessions, or SOUL.md. Picking a default model in the
  dashboard writes to the swarm's *own* shared config (`<data>/.hermes-shared`),
  not `~/.hermes`.

Precedence is: per-agent override → swarm default → your `~/.hermes` → proxy. Run
`hermes-swarm doctor` to see exactly which one is in effect.

---

## Configuration (environment variables)

> **Provider & model are configured with `hermes setup`, not env vars.** The
> `SWARM_LLM_*` variables below are a fully **opt-in** alternative for pointing
> the whole swarm at a single OpenAI-compatible / LiteLLM proxy — there is **no
> implicit proxy**. Leave them unset and the swarm reads your Hermes config; set
> `SWARM_LLM_BASE_URL` and the swarm routes every agent through that endpoint
> instead. Per-agent overrides (any provider/model) are set live from the dashboard.

| Variable | Default | Purpose |
|---|---|---|
| `SWARM_LLM_BASE_URL` | *(unset — proxy off)* | opt-in: OpenAI-compatible / LiteLLM endpoint for the whole swarm. Setting this enables the proxy path |
| `SWARM_LLM_API_KEY` | `sk-1234` | key for that proxy endpoint (only used when `SWARM_LLM_BASE_URL` is set) |
| `SWARM_DEFAULT_MODEL` | `litellm-model` | default model name when using the proxy path |
| `SWARM_FALLBACK_MODELS` | `litellm-model,kimi` | dropdown list if the backend can't be queried |
| `SWARM_HOST` / `SWARM_PORT` | `127.0.0.1` / `8000` | dashboard bind address |
| `SWARM_API_KEY` | *(unset)* | if set, required on **every** endpoint + WebSocket — the dashboard prompts for it once and remembers it. **Set it whenever you expose the port.** |
| `SWARM_DATA_DIR` | repo `./data` or `~/.hermes-swarm/data` | writable state (configs, queues, workspaces) |
| `HERMES_AGENT_PATH` | *(unset)* | path to a Hermes source checkout (only if not pip-installed) |

Per-agent settings (model, provider, tools, reasoning, iterations, sampling,
soul, autonomous wake-up interval, and **cron wake-ups**) are edited live from
the dashboard's ⚙️ panel — no restart needed. `SWARM_HEARTBEAT_SECONDS` is just
the default; each agent's idle wake-up cadence can be overridden in its settings.

**Scheduled wake-ups (cron):** every agent can run recurring work on a schedule
— a 9am competitor check, an hourly metrics pull, a Monday digest. Add/enable/
delete schedules from the ⚙️ panel (5-field cron, `@hourly`/`@daily`/`@weekly`/
`@monthly`, or intervals like `@every 30m`); agents can also self-schedule and
cancel their own via the `schedule_wakeup` / `cancel_wakeup` tools.

---

## Notes

- **Security:** by default the server binds `127.0.0.1` with no key. To expose
  it (VPS/LAN), set `SWARM_API_KEY` — it then guards every HTTP endpoint *and*
  the WebSocket, and the dashboard prompts for the key once (stored in your
  browser). Put it behind a TLS reverse proxy too. Agents can run terminal
  commands as the server user, so on a shared/exposed host prefer the Docker
  route. See [`docs/deploy-vps.md`](docs/deploy-vps.md) for a hardened setup.
- **Chromium** is required only for the browser-publishing tools; everything
  else works without it (the swarm degrades gracefully).
- **Web research:** agents get `web_search` + `web_extract`. Search works with no
  setup (DuckDuckGo). For *extract*, if you've configured a Hermes web backend
  (Firecrawl/Tavily/Exa/… via `hermes tools` or API-key env vars) the swarm uses
  it untouched; otherwise it falls back to a built-in fetcher — `httpx` out of
  the box (fine for normal pages), or install `pip install .[web]` (crawl4ai +
  `playwright install chromium`) for JavaScript-heavy single-page apps.
- **Data & backups:** state lives in `SWARM_DATA_DIR`; every config save keeps a
  rotating backup under `<data>/config_backups/`.
