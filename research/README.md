# Research chat

Personal research chatbot on the GB10 box. Uses whichever local LLM is running (`llm@fast` Qwen3.6-35B-A3B or
`llm@deep` gpt-oss-120b) through the vLLM OpenAI API; tools are MCP servers. Part of the Workbench repository
(since 2026-10-16, history kept) but a separate product: own service, own port, own data, own installer; the
Workbench `deploy.sh` does not install it. Shared with Workbench: the LLM stack, the offline library MCP and the
StarCraft-style skins.

```
browser ── :8090 ── app.py (FastAPI · SQLite history · uploads) ── vLLM 127.0.0.1:8000
                      └─ MCP (stdio): tools/web.py     web_search (local SearXNG :8888), fetch_url (HTML/PDF → text)
                                      tools/papers.py  arxiv_search, arxiv_paper (+ full text), zenodo_search, zenodo_record
                                      tools/scholar.py paper_search, doi_lookup (+ OA PDF link), references, cited_by
                                                       (OpenAlex; Crossref fallback)
                                      tools/github.py  gh_search_repos/_code/_issues, gh_repo, gh_read  (read-only)
                                      tools/machine.py run, job_start/_status/_stop, list_files, read_file,
                                                       write_file, edit_file, download   (as user `researcher`)
```

**Sources.** Search results are snippets; what the assistant actually *reads* (`fetch_url`, `arxiv_paper`,
`doi_lookup`, `zenodo_record`, `gh_repo`, `gh_read`, `library_read`, `download`) is saved per chat as source **S1, S2, …**
(`tools/sources.py`, tables `sources` / `source_parts` in `chats.db`): reference, URL, title, fetch times, which
parts were read (in the tool's own `offset` coordinates, or PDF pages) and a sha256 of the text read (of the file,
for downloads). The model cites `[S3]` (the chat links it), sees a one-line summary of every source with what it has
and has *not* read, and re-reads with `source_read(n, offset)` / `sources_list` once older tool output has been trimmed
from the prompt; an unread stretch comes back with the exact call that opens it. The **❞** button lists the
sources; the Markdown export appends them.

**Journal literature.** `tools/scholar.py` covers what arXiv does not: anything with a DOI. `paper_search`
searches OpenAlex works by title/abstract keywords (year range, `sort` relevance | cited | date, `open_access`
only); `doi_lookup` gives title, authors, venue, abstract, counts, open-access status and the best free PDF or
landing page (read it with `fetch_url`) and is recorded as source `doi:<doi>`; `references` / `cited_by` walk the
citation graph (most cited first, or newest first for `cited_by`), paged with `page`. Crossref fills in when
OpenAlex lacks a DOI, an abstract or a reference list, or is over its budget. OpenAlex needs no key, but without one
all requests from the box's IP share a free budget of about $0.10/day (a lookup ≈ $0.0001, a search ≈ $0.001,
so roughly 100 searches); a free personal key (`OPENALEX_API_KEY`, openalex.org/settings/api) gives its own $1/day.

**Tool loop.** Each MCP server runs in its own supervisor task: a crashed or hung server is restarted (read-only
tools are retried once; machine tools are not, they may have side effects). Arguments are checked before a call
(valid JSON, required fields, tool offered in this message — so Offline mode cannot be bypassed by a hallucinated
call); an identical read-only call in the same answer, or a third identical call in a row, is answered without
running it; three errors in a row add a hint to change approach. HTTP in the tools retries connection errors,
timeouts and 429/5xx (honouring `Retry-After`) and refuses pages over 60 MB. Every call is logged to `tool_calls`
(model, tool, args, ms, ok, note); `GET /api/tool-stats?days=30` aggregates errors, latency and repeats per model and
tool, `GET /api/status` shows the state of each tool server.

**Machine tools.** The assistant works in a research workspace on the box: writes and runs scripts (bash, up to
15 min per command; longer work as background jobs), downloads data, organises projects
(`<project>/data, scripts, results, notes.md`) and shows plots inline (`![](/ws/<path>)`). The **▦** button in
the sidebar browses the workspace; the **⌬ Machine** toggle turns these tools off for a message.

- Workspace `/var/lib/research/workspace` (group `researcher`, setgid; the chat serves it read-only at `/ws/`
  with a sandbox CSP). Python: `/var/lib/research/runner/venv` (numpy, pandas, scipy, xarray, netCDF4, dask,
  matplotlib, scikit-learn, statsmodels, … — `runner-requirements.txt`); `pip install` adds more. `copy_upload`
  puts an uploaded file into the workspace.
- **Sandbox.** The tool server and everything it starts run as the system user `researcher` (no sudo, no docker
  group, `no_new_privs`), started through one exact sudoers rule (`/etc/sudoers.d/92-research-runner`). It cannot
  read the operator's home, the chats or uploads (`/var/lib/research` 711, `chats.db` 600, `uploads/` 700).
  Outbound network fence `bin/runner-net` (`research-runner-net.service`, iptables owner match): internet, DNS and
  the LLM API `127.0.0.1:8000` only — no other local service (this chat, Workbench, OpenCode, SSH, docker), LAN or
  VPN. The tool server refuses to start if the fence is down. Memory: `MemoryMax=20G` for the chat and its jobs,
  so scripts cannot starve the LLM.
- Background jobs stop when the chat service restarts (deploys included).

**Features.** Chat history with search, rename, delete, Markdown export · file uploads · reactor menu (load /
unload `fast` | `deep` | `nemotron`; switching stops the running profile, loads take 5–10 min; also used by the
Workbench agent) · reasoning depth (off/low/med/high → `reasoning_effort` for gpt-oss, `enable_thinking` for
Qwen/Nemotron) · edit last message, regenerate, copy · context meter · standing orders (custom instructions) ·
KaTeX math · desktop launcher `~/Desktop/research.desktop`.

**Themes.** Two skins, switched with ◐ in the sidebar and kept in the browser: Terran (default; riveted gunmetal,
teal glass, microscope icon) and Zerg hive mind (chitin carapace, amber glow, creep; Overmind / Abathur wording).
The composer is a full-width bottom console like a StarCraft HUD; the top bar and sidebar have heavy frame edges,
menus and modals light frames. Frames and floor tiles are `static/frames/*.png` (2x, 9-slice `border-image`),
rendered by `art/frames.py` (numpy + pillow on the Mac, output committed; the Workbench UI uses copies of the
same files). App icons in `static/icons/` (`icon-*` Terran, `zerg-*` Zerg; the tab icon follows the theme).
Fonts offline: Tektur (Terran headings), Philosopher (Zerg headings), Play (text), JetBrains Mono.

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
port, web password for non-local browsers — user `research`, optional `GITHUB_TOKEN` / `ZENODO_TOKEN` /
`OPENALEX_API_KEY` / `OPENALEX_MAILTO`) and prints the password once; on an existing install add new keys by hand.
After editing it: `sudo systemctl restart research-chat`.

Services: `research-chat` (uvicorn, `0.0.0.0:8090`), `research-search` (SearXNG container `research-searxng`,
`127.0.0.1:8888`). Logs: `journalctl -u research-chat`, `docker logs research-searxng`.

## Local development

```bash
ssh -N -L 18000:127.0.0.1:8000 <box> &      # the box's LLM
LLM_URL=http://127.0.0.1:18000 RESEARCH_DATA=/tmp/rdata uvicorn app:app --port 18090
```

Without SearXNG `web_search` fails; the other tools work from anywhere. Without `RESEARCH_RUN_AS` the machine
tools run as you, in `$RESEARCH_DATA/workspace`, with no sandbox — development only.

Regenerating the theme art: `python3 -m venv /tmp/art && /tmp/art/bin/pip install numpy pillow && /tmp/art/bin/python
art/frames.py`, then copy `static/frames/*.png` to `../agent/ui/static/frames/` and bump the `?v=` in the CSS.
