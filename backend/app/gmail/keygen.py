"""Generate the Fernet key that encrypts the stored Gmail refresh token.

    python -m app.gmail.keygen                 print a new key once (paste it into .env as OAUTH_ENCRYPTION_KEY=...)
    python -m app.gmail.keygen --append-env    append OAUTH_ENCRYPTION_KEY=<new key> to the repository's .env WITHOUT printing it

--append-env refuses when the file already has a non-blank OAUTH_ENCRYPTION_KEY (replacing a key would make a stored credential
unreadable). Back the key up somewhere safe: losing it only means connecting Gmail again. Never commit it (.env is gitignored).

Exit codes: 0 done, 1 a key is already present.
"""
import argparse
import sys
from pathlib import Path

from cryptography.fernet import Fernet

from app.config import ROOT_DIR

ENV_NAME = "OAUTH_ENCRYPTION_KEY"


def has_key(env_text: str) -> bool:
    """True when the text assigns a non-blank OAUTH_ENCRYPTION_KEY (values are never returned or shown)."""
    for line in env_text.splitlines():
        name, sep, value = line.partition("=")
        if sep and name.strip() == ENV_NAME and value.strip().strip("'\""):
            return True
    return False


def append_key(env_file: Path) -> bool:
    """Append a new key to `env_file` (created if missing). Returns False, writing nothing, when a key is already there."""
    text = env_file.read_text(encoding="utf-8") if env_file.is_file() else ""
    if has_key(text):
        return False
    prefix = "" if not text or text.endswith("\n") else "\n"
    with env_file.open("a", encoding="utf-8", newline="\n") as f:
        f.write(f"{prefix}{ENV_NAME}={Fernet.generate_key().decode('ascii')}\n")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.gmail.keygen", description="Generate the Gmail token encryption key.")
    parser.add_argument("--append-env", action="store_true", help="append the key to .env without printing it")
    parser.add_argument("--env-file", type=Path, default=ROOT_DIR / ".env", help="the .env file to append to (default: the repo's)")
    args = parser.parse_args(argv)
    if not args.append_env:
        print(f"{ENV_NAME}={Fernet.generate_key().decode('ascii')}")
        print("Paste that line into the repository's .env (gitignored) and keep a backup copy. Shown once; not stored anywhere.")
        return 0
    if append_key(args.env_file):
        print(f"written: {ENV_NAME} appended to {args.env_file} (the value was not printed). Keep a backup copy of that line.")
        return 0
    print(f"NOT CHANGED: {args.env_file} already sets {ENV_NAME}. Replacing it would make a stored Gmail credential unreadable; "
          "remove the line yourself first if you really want a new key.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
