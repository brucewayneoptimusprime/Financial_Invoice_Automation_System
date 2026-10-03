"""Reading and changing the rules settings (the only writer of po_settings, po_rule_switches, settings_events, and of the global
rule params / switches / confidence threshold through the settings screen). Every changed value writes one settings_events row
(actor: unauthenticated demo user). A save that changes nothing writes nothing. Changes apply to future runs only."""
import json
import sqlite3
from typing import Any

from app.config import Settings, get_settings
from app.rulesettings import catalog
from app.rulesettings.catalog import BY_KEY, DEFS, FLOORS, LOCK_REASONS, SWITCHABLE_RULES, SettingsError

ACTOR = "unauthenticated demo user"


class NotFound(LookupError):
    pass


def _rules(conn: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT id, name, enabled FROM rules WHERE source = 'builtin' ORDER BY id")]


def _show(key: str, value: Any) -> str:
    if value is None:
        return "—"
    d = BY_KEY.get(key)
    if d is None:
        return "on" if value else "off"
    if d.kind == "percent":
        return f"{float(value):.2f}%"
    if d.kind == "mode":
        return "both limits" if value == "lesser_of" else "either limit"
    if d.kind == "days":
        return f"{value} days"
    return str(value)


def _log(conn: sqlite3.Connection, scope: str, po_id: int | None, key: str, old: Any, new: Any, message: str) -> None:
    conn.execute("INSERT INTO settings_events (scope, po_id, key, old_value, new_value, actor, message) VALUES (?, ?, ?, ?, ?, ?, ?)",
                 (scope, po_id, key, None if old is None else json.dumps(old), None if new is None else json.dumps(new), ACTOR, message))


def _rule_problems(conn: sqlite3.Connection, rules: dict[str, Any], allow_null: bool) -> dict[str, str]:
    known = {r["id"] for r in _rules(conn)}
    floors = {f[0] for f in FLOORS}
    problems = {}
    for rule_id, value in rules.items():
        if rule_id in floors:
            problems[f"rule:{rule_id}"] = "is an engine floor: always on, not a switch"
        elif rule_id not in known:
            problems[f"rule:{rule_id}"] = "is not a rule"
        elif value is None and allow_null:
            continue
        elif not isinstance(value, bool):
            problems[f"rule:{rule_id}"] = "must be true (on) or false (off)"
        elif catalog.is_locked(rule_id) and value is False:
            problems[f"rule:{rule_id}"] = "is locked and cannot be switched off"
    return problems


# ------------------------------------------------------------------------------------------ global defaults

def global_view(conn: sqlite3.Connection, settings: Settings | None = None) -> dict:
    s = settings or get_settings()
    current, builtin = catalog.global_values(conn, s), catalog.builtin_values(s)
    custom = conn.execute("SELECT COUNT(*) FROM (SELECT po_id FROM po_settings UNION SELECT po_id FROM po_rule_switches)").fetchone()[0]
    return {
        "values": [{"key": d.key, "label": d.label, "kind": d.kind, "help": d.help, "value": current[d.key], "builtin": builtin[d.key],
                    "min": None if d.minimum is None else str(d.minimum), "max": None if d.maximum is None else str(d.maximum),
                    "step": None if d.step is None else str(d.step), "options": list(catalog.MODES) if d.kind == "mode" else None}
                   for d in DEFS],
        "rules": [{"id": r["id"], "name": r["name"], "enabled": bool(r["enabled"]) or catalog.is_locked(r["id"]),
                   "locked": catalog.is_locked(r["id"]), "reason": LOCK_REASONS.get(r["id"]),
                   "switchable": r["id"] in SWITCHABLE_RULES} for r in _rules(conn)],
        "floors": [{"id": fid, "name": name, "reason": reason} for fid, name, reason in FLOORS],
        "custom_po_count": custom,
        "actor": ACTOR,
    }


def update_global(conn: sqlite3.Connection, *, values: dict | None = None, rules: dict | None = None, restore: list | None = None,
                  settings: Settings | None = None) -> dict:
    s = settings or get_settings()
    values, rules, restore = dict(values or {}), dict(rules or {}), list(restore or [])
    problems: dict[str, str] = {}
    for key in restore:
        if key not in BY_KEY:
            problems[key] = "is not an editable setting"
    try:
        clean = catalog.validate(values)
    except SettingsError as exc:
        problems.update(exc.problems)
        clean = {}
    problems.update(_rule_problems(conn, rules, allow_null=False))
    if problems:
        raise SettingsError(problems)
    builtin = catalog.builtin_values(s)
    for key in restore:
        clean[key] = builtin[key]
    current = catalog.global_values(conn, s)
    changed = {k: v for k, v in clean.items() if v != current[k]}
    switches = catalog.global_switches(conn)
    rule_changes = {r: v for r, v in rules.items() if switches.get(r) != v}
    conn.execute("BEGIN IMMEDIATE")
    try:
        params = {r["id"]: json.loads(r["params"]) for r in conn.execute("SELECT id, params FROM rules")}
        touched: set[str] = set()
        for key, value in changed.items():
            d = BY_KEY[key]
            if d.rule_id is None:
                conn.execute("INSERT INTO settings (key, value) VALUES ('confidence_threshold', ?) "
                             "ON CONFLICT (key) DO UPDATE SET value = excluded.value", (json.dumps(value),))
            else:
                params[d.rule_id][d.param] = float(value) if d.kind in ("money", "percent") else value
                touched.add(d.rule_id)
            verb = "restored to the built-in default" if key in restore else "changed"
            _log(conn, "global", None, key, current[key], value, f"{d.label} (global default) {verb}: {_show(key, current[key])} → {_show(key, value)}.")
        for rule_id in touched:
            conn.execute("UPDATE rules SET params = ? WHERE id = ?", (json.dumps(params[rule_id]), rule_id))
        for rule_id, value in rule_changes.items():
            conn.execute("UPDATE rules SET enabled = ? WHERE id = ?", (int(value), rule_id))
            _log(conn, "global", None, f"rule:{rule_id}", switches.get(rule_id), value,
                 f"Rule {rule_id} (global default) switched {'on' if value else 'off'}.")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return {**global_view(conn, s), "changed": len(changed) + len(rule_changes)}


# ------------------------------------------------------------------------------------------ per-PO overrides

def _po(conn: sqlite3.Connection, po_id: int) -> dict:
    row = conn.execute("SELECT po.id, po.po_number, po.currency, po.status, v.name AS vendor FROM purchase_orders po "
                       "JOIN vendors v ON v.id = po.vendor_id WHERE po.id = ?", (po_id,)).fetchone()
    if row is None:
        raise NotFound(po_id)
    return dict(row)


def _po_summary(conn: sqlite3.Connection, po_id: int, defaults: dict, switches: dict) -> tuple[int, bool]:
    over, sw = catalog.po_override_values(conn, po_id), catalog.po_override_switches(conn, po_id)
    looser = any(catalog.is_looser(k, v, defaults[k]) for k, v in over.items()) or \
        any(v is False and switches.get(r, True) for r, v in sw.items())
    return len(over) + len(sw), looser


def po_list(conn: sqlite3.Connection, q: str | None = None, custom: bool | None = None, settings: Settings | None = None) -> list[dict]:
    defaults, switches = catalog.global_values(conn, settings), catalog.global_switches(conn)
    sql = ("SELECT po.id, po.po_number, po.currency, po.status, v.name AS vendor FROM purchase_orders po JOIN vendors v ON v.id = po.vendor_id")
    args: list = []
    if q:
        sql += " WHERE po.po_number LIKE ? OR v.name LIKE ?"
        args = [f"%{q}%", f"%{q}%"]
    out = []
    for r in conn.execute(sql + " ORDER BY po.id DESC", args):
        n, looser = _po_summary(conn, r["id"], defaults, switches)
        if custom is not None and (n > 0) != custom:
            continue
        out.append({**dict(r), "overrides": n, "custom": n > 0, "looser": looser})
    return out


def po_view(conn: sqlite3.Connection, po_id: int, settings: Settings | None = None) -> dict:
    po = _po(conn, po_id)
    defaults, over = catalog.global_values(conn, settings), catalog.po_override_values(conn, po_id)
    gsw, psw = catalog.global_switches(conn), catalog.po_override_switches(conn, po_id)
    values = []
    for d in DEFS:
        overridden = d.key in over
        values.append({"key": d.key, "label": d.label, "kind": d.kind, "help": d.help, "default": defaults[d.key],
                       "value": over[d.key] if overridden else defaults[d.key], "source": "overridden" if overridden else "inherits",
                       "looser": overridden and catalog.is_looser(d.key, over[d.key], defaults[d.key]),
                       "min": None if d.minimum is None else str(d.minimum), "max": None if d.maximum is None else str(d.maximum),
                       "step": None if d.step is None else str(d.step), "options": list(catalog.MODES) if d.kind == "mode" else None})
    rules = []
    for r in _rules(conn):
        locked = catalog.is_locked(r["id"])
        overridden = r["id"] in psw and not locked
        effective = True if locked else (psw[r["id"]] if overridden else gsw[r["id"]])
        rules.append({"id": r["id"], "name": r["name"], "default": True if locked else gsw[r["id"]], "enabled": effective,
                      "source": "overridden" if overridden else "inherits", "locked": locked, "reason": LOCK_REASONS.get(r["id"]),
                      "looser": overridden and effective is False and gsw[r["id"]] is True})
    return {"po": po, "values": values, "rules": rules, "floors": [{"id": f, "name": n, "reason": why} for f, n, why in FLOORS],
            "overrides": len(over) + len(psw), "looser": any(v["looser"] for v in values) or any(r["looser"] for r in rules)}


def update_po(conn: sqlite3.Connection, po_id: int, *, values: dict | None = None, rules: dict | None = None,
              settings: Settings | None = None) -> dict:
    po = _po(conn, po_id)
    values, rules = dict(values or {}), dict(rules or {})
    problems: dict[str, str] = {}
    try:
        clean = catalog.validate({k: v for k, v in values.items() if v is not None})
    except SettingsError as exc:
        problems.update(exc.problems)
        clean = {}
    for key in (k for k, v in values.items() if v is None and k not in BY_KEY):
        problems[key] = "is not an editable setting"
    problems.update(_rule_problems(conn, rules, allow_null=True))
    if problems:
        raise SettingsError(problems)
    resets = [k for k, v in values.items() if v is None]
    defaults, before = catalog.global_values(conn, settings), catalog.po_override_values(conn, po_id)
    gsw, psw = catalog.global_switches(conn), catalog.po_override_switches(conn, po_id)
    label = po["po_number"]
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("INSERT OR IGNORE INTO po_settings (po_id) VALUES (?)", (po_id,))
        for key, value in clean.items():
            if before.get(key) == value:
                continue
            d = BY_KEY[key]
            conn.execute(f"UPDATE po_settings SET {d.column} = ?, updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE po_id = ?",
                         (catalog.to_column(key, value), po_id))
            was = f"overridden {_show(key, before[key])}" if key in before else f"inherits {_show(key, defaults[key])}"
            _log(conn, "po", po_id, key, before.get(key), value, f"{d.label} for {label}: {was} → {_show(key, value)}.")
        for key in resets:
            if key not in before:
                continue
            d = BY_KEY[key]
            conn.execute(f"UPDATE po_settings SET {d.column} = NULL, updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE po_id = ?",
                         (po_id,))
            _log(conn, "po", po_id, key, before[key], None,
                 f"{d.label} for {label}: overridden {_show(key, before[key])} → reset to the default ({_show(key, defaults[key])}).")
        cols = " AND ".join(f"{d.column} IS NULL" for d in DEFS)
        conn.execute(f"DELETE FROM po_settings WHERE po_id = ? AND {cols}", (po_id,))
        for rule_id, value in rules.items():
            if catalog.is_locked(rule_id):
                continue                                                 # "on" for a locked rule is already the only state
            if value is None:
                if rule_id in psw:
                    conn.execute("DELETE FROM po_rule_switches WHERE po_id = ? AND rule_id = ?", (po_id, rule_id))
                    _log(conn, "po", po_id, f"rule:{rule_id}", psw[rule_id], None,
                         f"Rule {rule_id} for {label}: reset to the default ({'on' if gsw[rule_id] else 'off'}).")
                continue
            if psw.get(rule_id) == value:
                continue
            conn.execute("INSERT INTO po_rule_switches (po_id, rule_id, enabled) VALUES (?, ?, ?) ON CONFLICT (po_id, rule_id) DO UPDATE "
                         "SET enabled = excluded.enabled, updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')", (po_id, rule_id, int(value)))
            _log(conn, "po", po_id, f"rule:{rule_id}", psw.get(rule_id), value,
                 f"Rule {rule_id} for {label}: switched {'on' if value else 'off'}.")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return po_view(conn, po_id, settings)


def history(conn: sqlite3.Connection, scope: str | None = None, po_id: int | None = None, limit: int = 50) -> list[dict]:
    sql, args = ("SELECT e.*, p.po_number FROM settings_events e LEFT JOIN purchase_orders p ON p.id = e.po_id WHERE 1 = 1", [])
    if scope in ("global", "po"):
        sql += " AND e.scope = ?"
        args.append(scope)
    if po_id is not None:
        sql += " AND e.po_id = ?"
        args.append(po_id)
    rows = conn.execute(sql + " ORDER BY e.id DESC LIMIT ?", (*args, max(1, min(limit, 500)))).fetchall()
    return [{"id": r["id"], "scope": r["scope"], "po_id": r["po_id"], "po_number": r["po_number"], "key": r["key"],
             "old_value": None if r["old_value"] is None else json.loads(r["old_value"]),
             "new_value": None if r["new_value"] is None else json.loads(r["new_value"]), "actor": r["actor"],
             "message": r["message"], "created_at": r["created_at"]} for r in rows]
