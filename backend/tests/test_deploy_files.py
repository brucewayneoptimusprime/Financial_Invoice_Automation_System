"""The deployment files (deployment stage 3): render.yaml, frontend/vercel.json, DEPLOY.md. Checked as text so no YAML library is
needed: no secret values, no automatic reset/init/migrate, the approved settings present."""
import json
import re

from app.config import ROOT_DIR

RENDER = (ROOT_DIR / "render.yaml").read_text(encoding="utf-8")
DEPLOY = (ROOT_DIR / "DEPLOY.md").read_text(encoding="utf-8")


def env(key):
    m = re.search(rf"- key: {key}\n\s+(value|sync): (.+?)\s*(#.*)?\n", RENDER)
    return None if m is None else (m.group(1), m.group(2).strip().strip('"'))


def test_the_start_command_never_creates_resets_or_migrates_the_database():
    start = re.search(r"startCommand: (.+)", RENDER).group(1)
    assert start.split("#")[0].strip() == "python -m app.api.serve --host 0.0.0.0 --port $PORT"
    code = "\n".join(line.split("#")[0] for line in RENDER.splitlines())
    for word in ("reset", "init_db", "migrate", "--reset-demo"):
        assert word not in code, word


def test_secrets_are_never_in_the_file():
    for key in ("ANTHROPIC_API_KEY", "ACCESS_TOKEN", "API_CORS_ORIGINS"):
        assert env(key) == ("sync", "false"), key
    assert "sk-ant" not in RENDER


def test_the_approved_settings():
    assert env("SERVE_MODE") == ("value", "live")
    assert env("DATA_DIR") == ("value", "/var/data")
    assert env("COST_CEILING_PER_SESSION_USD") == ("value", "1.00")
    assert env("PYTHON_VERSION") == ("value", "3.12.7")
    assert re.search(r"healthCheckPath: /health\b", RENDER)
    assert re.search(r"disk:\n\s+name: invoice-agent-data\n\s+mountPath: /var/data\n\s+sizeGB: 1", RENDER)
    assert re.search(r"plan: starter", RENDER)


def test_the_disk_mount_is_the_data_dir():
    assert re.search(r"mountPath: (\S+)", RENDER).group(1) == env("DATA_DIR")[1]


def test_vercel_json():
    cfg = json.loads((ROOT_DIR / "frontend" / "vercel.json").read_text(encoding="utf-8"))
    assert cfg == {"buildCommand": "npm run build", "outputDirectory": "dist", "framework": "vite",
                   "rewrites": [{"source": "/((?!assets/).*)", "destination": "/index.html"}]}


def test_deploy_md_names_every_variable_and_the_manual_steps():
    for text in ("ANTHROPIC_API_KEY", "ACCESS_TOKEN", "API_CORS_ORIGINS", "API_CORS_ORIGIN_REGEX", "SERVE_MODE", "DATA_DIR",
                 "COST_CEILING_PER_SESSION_USD", "VITE_API_BASE", "Root Directory: `frontend`", "python -m app.db.reset --demo",
                 "python -m app.db.init_db", "python -m app.db.migrate", "/health"):
        assert text in DEPLOY, text
    assert "never" in DEPLOY and "automatically" in DEPLOY


def test_the_frontend_env_example_leaves_the_api_base_empty():
    example = (ROOT_DIR / "frontend" / ".env.example").read_text(encoding="utf-8")
    assert re.search(r"^VITE_API_BASE=$", example, re.M)
