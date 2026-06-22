# SEO Analyst Agent (Marketing Department)

Backend home for the **SEO Analyst** agent (frontend card `a2`, category `seo`:
*"Audits pages, finds keyword gaps, and writes optimization briefs."*).

**Status: scaffold.** The folder and importable package exist; capabilities,
pipeline, and module layout are still being designed. Not yet wired into the app
(`app/__init__.py`) or exposed via a router, and `live: False` in
`app/services/agent_config.py`.

## Layout (current)

```
agents/SEO agent/
├── README.md            # this file
└── seo_agent/           # importable package (underscore name)
    └── __init__.py
```

## Convention

Mirrors `Graphics designer agent`: the outer folder name has spaces, so once we
go live `app/__init__.py` will put this folder on `sys.path` and code will
`from seo_agent import ...`.

## Next

Define the agent's capabilities and pipeline (see the in-conversation design
discussion), then build out the package modules and a `seo` router.
