# Credentials: where Render keys go, and why

## The rule: secrets never live inside a skill

A skill is a **portable, shareable, version-controlled artifact** — it can be packaged
into a `.skill` file and installed elsewhere. The Agent Skills standard has **no secrets
mechanism**, by design: bundling a credential would mean shipping it wherever the skill
goes. A `.gitignore` stops a git commit but **not** packaging (`package_skill.py` zips
the whole folder) or a plain `cp -r`. Installed skill directories are also often
read-only. So: **a skill references credentials from the environment; it never contains
them.**

## The pattern this skill uses (CLI/API access)

Keys live in an **external** git-ignored file, one per account, keyed by label:

```
# ~/.config/render/accounts.env   (chmod 600; override path with RENDER_ACCOUNTS_ENV)
RENDER_API_KEY_PERSONAL=rnd_xxx
RENDER_API_KEY_CLIENTB=rnd_yyy
```

`scripts/render.sh` sources that file, exports the keys into the **subprocess**
environment, and execs the CLI:

```sh
set -a; . "$ENV_FILE"; set +a      # keys -> env
exec python3 render_ops.py "$@"
```

Two properties this gives you:

1. **The key values never enter the agent's context.** The agent runs
   `render.sh migrate ...`; the CLI reads the keys from its own environment; nothing
   prints them. They stay out of the transcript, logs, and any saved output.
2. **Rotation and scale are trivial.** Any number of accounts — add a line. A rotated
   key — edit a line. The skill never changes, because it only knows *labels*, not
   values.

Real environment variables take precedence over the file, so CI can inject
`RENDER_API_KEY_*` directly without the file existing.

## "Why not a .env inside the skill?"

It's the right *pattern* (load + inject, don't hand-read) but the wrong *location*.
Inside the skill, the file rides along when the skill is packaged or copied, and you
may not be able to write it in a read-only install. Moving it **outside** the skill
keeps the loader's benefits while making the skill safe to share.

## MCP-based access is different

If a skill reaches a service through an **MCP server** rather than a CLI/HTTP call, the
credential belongs to the **MCP server's configuration** (the client's MCP config / the
server's own env), **not the skill**. The server authenticates; the skill just calls the
tools and never sees the key. If the key rotates, you update the MCP server config and
the skill is untouched. A well-formed MCP-using skill simply states "requires the X MCP
server to be connected" and carries no credential logic at all.

**Summary:** CLI/API skill → external env file + wrapper (this skill). MCP skill → let
the server hold the credential. Either way, nothing secret lives in the skill.
