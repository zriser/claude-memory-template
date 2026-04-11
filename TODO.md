# claude-memory — Roadmap & Todo List

## 🔥 This Week (let it prove itself)
- [ ] Use Claude Code normally for a few days — confirm flush.py runs on session exit
- [ ] Check ~/claude-memory/sessions/flush.log after first real session
- [ ] Check daily/ for auto-generated logs
- [ ] Run mem-save after 2-3 sessions to push first real content to GitHub
- [ ] Review extraction quality — is flush catching the right stuff?

## 🔧 Short Term (next 2 weeks)
- [ ] Fix the broken wiki-link slug from smoke test (compile agent linking issue)
- [ ] Run lint.py after a week of real usage, fix any issues it finds
- [ ] Deploy to a second environment (git clone + bootstrap-memory.sh)
- [ ] Set up GitHub MCP server for real (config snippet is in brain/mcp-servers/github-mcp.md)
- [ ] Add first real skills/slash commands and catalog them in brain/skills/
- [ ] Auto-commit: cron job or git hook so you don't have to remember mem-save
  - e.g. cron: `0 22 * * * cd ~/claude-memory && git add -A && git commit -m "vault: $(date +\%Y-\%m-\%d)" --allow-empty && git push`

## 🧩 Medium Term (month+)
- [ ] SilverBullet viewer on Proxmox — Docker container pointed at vault files
  - Syncthing or git pull cron to keep in sync
  - Browser-based access from any device including iPad
  - Add SilverBullet MCP server for programmatic access
- [ ] LiteLLM integration — swap models for different scripts
  - flush.py → cheap/fast model (Gemini Flash, local Ollama)
  - compile.py → smarter model (Claude Sonnet, GPT-4o)
  - query.py → user's choice per query
  - Could run LiteLLM as Docker container alongside SilverBullet on Proxmox
- [ ] Integrate with claude-context repo
  - Have session-start.py pull relevant context from both repos
  - Single bootstrap that sets up both claude-context AND claude-memory
- [ ] Per-project memory scoping
  - When working in a specific project, auto-filter vault to project-relevant notes
  - Tag system: #project/myproject, #project/work, #personal

## 🚀 Advanced (when the vault is mature)
- [ ] Semantic search (basic-memory style)
  - Add FastEmbed or similar for vector search over the vault
  - Falls back to grep/ripgrep when embeddings unavailable
  - Useful once vault exceeds ~200 articles where index-based retrieval gets noisy
- [ ] Query from claude.ai (not just Claude Code)
  - MCP server that exposes the vault to claude.ai chat sessions
  - Would let you do what we did today but with your vault context loaded
- [ ] Karpathy-style raw/ ingestion pipeline
  - Drop articles, papers, bookmarks into raw/
  - LLM compiles them into wiki articles automatically
  - Obsidian Web Clipper or a bookmarklet to capture web pages
- [ ] Cross-environment dashboards
  - SilverBullet Lua queries showing: recent sessions, active projects,
    decision log, mistake patterns
  - Basically an auto-generated "state of your work" page
- [ ] Team vault
  - Separate vault under a shared org
  - Shared patterns, architecture decisions, domain knowledge
  - Each team member runs their own flush but all compile into shared wiki
- [ ] Fine-tuning (Karpathy's endgame)
  - Once vault is large enough, use it as training data
  - Fine-tune a smaller model that "knows" your personal knowledge base
  - Long-term moonshot but the data is accumulating from day one

## 💡 Ideas (unvalidated, might be dumb)
- [ ] Voice memo ingestion — transcribe voice notes, flush into vault
- [ ] n8n integration — webhook on git push triggers compile
- [ ] Daily email/slack digest of what the vault learned yesterday
- [ ] "What did I work on this week?" query as a Friday ritual
- [ ] Connect to Google Calendar MCP — auto-tag daily logs with meeting context
- [ ] Obsidian publish — static site of your wiki (public or private)