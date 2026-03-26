# Contributing to Brain

Thank you for your interest. Brain is a solo-built project
and contributions are welcome.

## Before you start

Read CLAUDE.md. It documents the full architecture,
critical rules, and patterns. A 15-minute read that
saves hours of confusion.

## Key things to know

**The brain.json file is the source of truth.**
Never hardcode `data/brain.json` — use `get_brain_path()`.
Never save an empty brain over a populated one.
Never change a node ID after creation.

**Bump ?v=N on every frontend change.**
All static files are cache-busted with a version query param.
Increment the number in index.html on every JS/CSS change.

**Never auto-apply destructive operations.**
Merges and deletes always queue to Act for user confirmation.

## Development setup

```bash
git clone https://github.com/loadingpeacefully/brain-project
cd brain-project
python -m venv venv
source venv/bin/activate
pip install fastapi uvicorn anthropic pypdf python-docx \
            python-multipart python-dotenv pydantic numpy \
            apscheduler sentence-transformers
echo "ANTHROPIC_API_KEY=your_key_here" > .env
python backend/main.py
```

## What to work on

Check [Issues](https://github.com/loadingpeacefully/brain-project/issues)
for open bugs and feature requests.

High-value areas:
- New document parser formats (EPUB, HTML, Markdown with frontmatter)
- Graph visualization improvements
- BHS formula refinements
- Test coverage (currently none — anything helps)
- SQLite migration from brain.json

## Pull request process

1. Fork the repo
2. Create a branch: `git checkout -b feature/your-feature`
3. Make changes following the patterns in CLAUDE.md
4. Test manually — there are no automated tests yet
5. Submit PR with a clear description of what changed and why

## Code style

Python: follow existing patterns, no strict linter enforced.
JavaScript: vanilla JS, no frameworks, match existing style.
No TypeScript. No build step. Keep it simple.
