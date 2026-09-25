"""Start the local API server.

    python -m app.api.serve (--replay DIR | --live [--record DIR] | --offline) [--db PATH] [--reset-demo] [--host H] [--port N]

LIVE CALLS NEED --live. The server REFUSES to start unless exactly one mode is given (exit 5, before any client exists):
  --replay DIR   serve recorded model responses only; nothing is sent, nothing is spent
  --live         real, paid API calls (about $0.03 per invoice); the per-run and per-session ceilings apply to this process
  --offline      no model at all: extraction degrades to review, explanations are templates (for UI work)

Exit codes: 0 stopped, 2 usage, 3 API key not configured (live), 5 no mode given (live call refused).
"""
import argparse
import sys
from decimal import Decimal
from pathlib import Path

from app.api.clients import build_api_client
from app.config import get_settings
from app.db.reset import reset_database
from app.llm.budget import CostTracker
from app.llm.errors import LLMConfigError

EXIT_USAGE, EXIT_NOT_CONFIGURED, EXIT_LIVE_REFUSED = 2, 3, 5
ESTIMATED_COST = Decimal("0.03")


def refusal_message() -> str:
    return ("REFUSED: the server was not started (nothing was sent, nothing was spent).\n"
            "Choose exactly one mode:\n"
            "  python -m app.api.serve --replay data\\recordings    recorded responses only (free)\n"
            "  python -m app.api.serve --offline                   no model at all (free)\n"
            f"  python -m app.api.serve --live [--record DIR]      real, paid API calls (about ${ESTIMATED_COST} per invoice)")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m app.api.serve", description="Start the local invoice-agent API.")
    p.add_argument("--replay", type=Path, help="serve responses recorded in DIR (no API call, no key needed)")
    p.add_argument("--live", action="store_true", help="ALLOW real, paid API calls")
    p.add_argument("--offline", action="store_true", help="no model calls at all")
    p.add_argument("--record", type=Path, help="with --live: also record each response into DIR")
    p.add_argument("--db", type=Path, help="SQLite database (default from config)")
    p.add_argument("--reset-demo", action="store_true", help="first reset the database to the demo dataset")
    p.add_argument("--host", help="bind address (default from config: 127.0.0.1)")
    p.add_argument("--port", type=int, help="port (default from config: 8000)")
    return p


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    args = build_parser().parse_args(argv)
    modes = [m for m, on in (("live", args.live), ("replay", args.replay is not None), ("offline", args.offline)) if on]
    if not modes:
        print(refusal_message())
        return EXIT_LIVE_REFUSED
    if len(modes) > 1:
        print(f"BAD USAGE: choose exactly one of --live, --replay DIR, --offline (got {', '.join(modes)}).")
        return EXIT_USAGE
    mode = modes[0]
    if args.record is not None and mode != "live":
        print("BAD USAGE: --record needs --live.")
        return EXIT_USAGE
    if mode == "replay" and not args.replay.is_dir():
        print(f"BAD USAGE: --replay {args.replay} is not a folder of recorded responses.")
        return EXIT_USAGE

    settings = get_settings()
    db_path = args.db or settings.db_path
    if args.reset_demo:
        try:
            reset_database(db_path, settings.demo_seed_path)
        except ValueError as exc:
            print(f"BAD USAGE: {exc}")
            return EXIT_USAGE
        print(f"Database reset to the demo dataset: {db_path}")
    elif not Path(db_path).is_file():
        print(f"The database {db_path} does not exist. Create it with:  python -m app.db.reset --demo   (or pass --reset-demo)")
        return EXIT_USAGE

    tracker = CostTracker(settings.cost_ceiling_per_run_usd, settings.cost_ceiling_per_session_usd)
    try:
        client = build_api_client(mode, settings, tracker, replay=args.replay, record=args.record)
    except LLMConfigError as exc:
        print(f"NOT CONFIGURED: {exc.message}\n(Use --replay DIR or --offline to run without the API.)")
        return EXIT_NOT_CONFIGURED

    host, port = args.host or settings.api_host, args.port or settings.api_port
    banner = {"live": f"LIVE MODE: every upload calls the paid API (about ${ESTIMATED_COST} per invoice; ceilings "
                      f"${settings.cost_ceiling_per_run_usd} per run, ${settings.cost_ceiling_per_session_usd} for this server process).",
              "replay": f"REPLAY MODE: recorded responses from {args.replay}; no API calls (a miss degrades the run, the explainer and "
                        "drafter fall back to templates).",
              "offline": "OFFLINE MODE: no model calls; extraction degrades to review and explanations are templates."}[mode]
    print(banner)
    print(f"Database: {db_path}\nAPI: http://{host}:{port}/api   (UI: run `npm run dev` in frontend/, then open http://localhost:5173)")

    import uvicorn

    from app.api.main import create_app
    app = create_app(settings, mode=mode, client=client, db_path=Path(db_path), tracker=tracker, replay_dir=args.replay)
    uvicorn.run(app, host=host, port=port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
