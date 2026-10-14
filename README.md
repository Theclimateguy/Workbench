# Research chat

Personal research chatbot on the GB10 box. Uses whichever local LLM is running (`llm@fast` Qwen3.6-35B-A3B or
`llm@deep` gpt-oss-120b) through the vLLM OpenAI API; tools are MCP servers. Separate from the Workbench support
agent: own service, own port, own data.

```
browser ── :8090 ── app.py (FastAPI · SQLite history · uploads) ── vLLM 127.0.0.1:8000
                      └─ MCP (stdio): tools/web.py     web_search (local SearXNG :8888), fetch_url (HTML/PDF → text)
                                      tools/papers.py  arxiv_search, arxiv_paper (+ full text), zenodo_search, zenodo_record
                                      tools/github.py  gh_search_repos/_code/_issues, gh_repo, gh_read  (read-only)
```

**Features.** Chat history with search, rename, delete, Markdown export · file uploads · reactor menu (load /
unload `fast` | `deep` | `nemotron`; switching stops the running profile, loads take 5–10 min; also used by the
Workbench agent) · reasoning depth (off/low/med/high → `reasoning_effort` for gpt-oss, `enable_thinking` for
Qwen/Nemotron) · edit last message, regenerate, copy · context meter · standing orders (custom instructions) ·
KaTeX math · Terran HUD theme, offline fonts · desktop launcher `~/Desktop/research.desktop`.

**Privacy.** Chats (`/var/lib/research/chats.db`) and uploaded files (`/var/lib/research/uploads/`) stay on the box and
go only to the local LLM. What leaves the box: search queries and paper/repo/page lookups the model makes. The
system prompt forbids putting file contents into tool arguments; the **Search / Offline** toggle in the composer
turns all internet tools off for a message.

**Files.** PDF, DOCX, HTML and any text/code format, up to 50 MB. The first 40k chars of each file go into the
prompt; the model reads the rest with `read_upload`. Images are not supported.

## Install / update (on the box)

```bash
~/Workbench/research/install.sh
```

From the Mac: `make deploy HOST=<ssh target>` (in this folder). First install creates `/etc/research/research.env` (bind address,
port, web password for non-local browsers — user `research`, optional `GITHUB_TOKEN` / `ZENODO_TOKEN`) and prints the
password once. After editing it: `sudo systemctl restart research-chat`.

Services: `research-chat` (uvicorn, `0.0.0.0:8090`), `research-search` (SearXNG container `research-searxng`,
`127.0.0.1:8888`). Logs: `journalctl -u research-chat`, `docker logs research-searxng`.

## Local development

```bash
ssh -N -L 18000:127.0.0.1:8000 <box> &      # the box's LLM
LLM_URL=http://127.0.0.1:18000 RESEARCH_DATA=/tmp/rdata uvicorn app:app --port 18090
```

Without SearXNG `web_search` fails; the other tools work from anywhere.
