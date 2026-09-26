"""The deployment files: render.yaml (Render FREE instance type), frontend/vercel.json, DEPLOY.md. Checked as text so no YAML
library is needed: no secret values, the server's start never creates/resets/migrates, the demo database is seeded by the BUILD
(the free tier has no Shell and no disk), the approved settings present."""
import json
import re

from app.config import ROOT_DIR

RENDER = (ROOT_DIR / "render.yaml").read_text(encoding="utf-8")
DEPLOY = (ROOT_DIR / "DEPLOY.md").read_text(encoding="utf-8")
CODE = "\n".join(line.split("#")[0] for line in RENDER.splitlines())      # render.yaml without its comments


def env(key):
    m = re.search(rf"- key: {key}\n\s+(value|sync): (.+?)\s*(#.*)?\n", RENDER)
    return None if m is None else (m.group(1), m.group(2).strip().strip('"'))


def command(name):
    return re.search(rf"{name}: (.+)", RENDER).group(1).split("#")[0].strip()


def test_the_start_command_never_creates_resets_or_migrates_the_database():
    start = command("startCommand")
    assert start == "python -m app.api.serve --host 0.0.0.0 --port $PORT"
    for word in ("reset", "init_db", "migrate"):
        assert word not in start, word


def test_the_build_command_seeds_the_demo_database_and_nothing_migrates():
    """Free tier: no Shell, no disk. The build output is what every start begins from, so the demo database is created there."""
    assert command("buildCommand") == "pip install -e . && python -m app.db.reset --demo"
    assert "migrate" not in CODE and "--reset-demo" not in CODE


def test_secrets_are_never_in_the_file():
    for key in ("ANTHROPIC_API_KEY", "ACCESS_TOKEN", "API_CORS_ORIGINS"):
        assert env(key) == ("sync", "false"), key
    assert "sk-ant" not in RENDER


def test_the_approved_settings():
    assert env("SERVE_MODE") == ("value", "live")
    assert env("DATA_DIR") == ("value", "data/render")              # relative to the repository root: inside the build output
    assert env("COST_CEILING_PER_SESSION_USD") == ("value", "1.00")
    assert env("PYTHON_VERSION") == ("value", "3.12.7")
    assert re.search(r"healthCheckPath: /health\b", RENDER)
    assert re.search(r"^\s+plan: free\s*$", RENDER, re.M)


def test_there_is_no_disk_on_the_free_tier():
    assert "disk:" not in CODE and "mountPath" not in CODE and "/var/data" not in CODE


def test_the_local_equivalent_of_the_data_dir_is_gitignored():
    assert re.search(r"^data/render/$", (ROOT_DIR / ".gitignore").read_text(encoding="utf-8"), re.M)


def test_vercel_json():
    cfg = json.loads((ROOT_DIR / "frontend" / "vercel.json").read_text(encoding="utf-8"))
    assert cfg == {"buildCommand": "npm run build", "outputDirectory": "dist", "framework": "vite",
                   "rewrites": [{"source": "/((?!assets/).*)", "destination": "/index.html"}]}


def test_deploy_md_names_every_variable_and_the_free_tier_facts():
    for text in ("ANTHROPIC_API_KEY", "ACCESS_TOKEN", "API_CORS_ORIGINS", "API_CORS_ORIGIN_REGEX", "SERVE_MODE", "DATA_DIR",
                 "COST_CEILING_PER_SESSION_USD", "VITE_API_BASE", "Root Directory: `frontend`", "python -m app.db.reset --demo",
                 "python -m app.db.migrate", "/health", "15 minutes", "Manual Deploy", "data/render", "no Shell"):
        assert text in DEPLOY, text
    assert DEPLOY.count("## What is lost, and when") == 1                   # the ephemeral caveat lives in ONE place


def test_the_frontend_env_example_leaves_the_api_base_empty():
    example = (ROOT_DIR / "frontend" / ".env.example").read_text(encoding="utf-8")
    assert re.search(r"^VITE_API_BASE=$", example, re.M)
