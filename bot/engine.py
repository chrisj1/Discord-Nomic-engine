"""
Patch validation, application, and rule loading.

Only the engine (not rules.py) is allowed to use subprocess, os, etc.
"""

import ast
import importlib.util
import logging
import os
import re
import subprocess
import tempfile
import types
from pathlib import Path

log = logging.getLogger(__name__)

# ── Forbidden AST nodes ───────────────────────────────────────────────────────

_FORBIDDEN_MODULES = frozenset({
    "os", "sys", "io", "subprocess", "socket", "urllib", "http",
    "requests", "httpx", "aiohttp", "pathlib", "shutil", "glob",
    "pickle", "marshal", "shelve", "dbm", "sqlite3", "importlib",
    "builtins", "ctypes", "threading", "multiprocessing", "concurrent",
    "asyncio", "signal", "mmap", "select", "selectors", "ssl",
    # hashlib / hmac removed: pure computation, useful for rules that want
    # deterministic checksums (e.g. proof-of-work-style patch constraints).
    "secrets", "tempfile", "fnmatch", "linecache",
    "tokenize", "compileall", "py_compile", "zipfile", "tarfile",
    "gzip", "bz2", "lzma", "zlib", "struct", "codecs", "ftplib",
    "smtplib", "telnetlib", "xmlrpc", "email", "html", "xml",
    "csv", "configparser", "logging", "unittest", "doctest",
    "pdb", "profile", "cProfile", "timeit", "trace", "gc",
    "inspect", "dis", "ast", "symtable", "token", "keyword",
    "pty", "tty", "termios", "fcntl", "grp", "pwd", "resource",
    "readline", "rlcompleter", "curses", "tkinter", "wx",
})

_FORBIDDEN_BUILTINS = frozenset({
    # Code execution
    "open", "exec", "eval", "compile", "__import__", "breakpoint",
    "input", "print",
    # Introspection / sandbox-escape vectors
    "getattr", "setattr", "delattr", "hasattr",
    "vars", "dir", "globals", "locals", "help",
    # Type construction (can be used to build new types with hidden behavior)
    "type", "object",
    # Bypassing __subclasses__ / metaclass tricks
    "super", "classmethod", "staticmethod",
})

# Attribute access to these names is forbidden — they're the primary tools used
# to escape AST-based sandboxes (walking the class hierarchy, reaching builtins,
# etc.). Blocking dunder access wholesale catches both known and future tricks.
_FORBIDDEN_ATTRS = frozenset({
    "__class__", "__bases__", "__mro__", "__subclasses__", "__base__",
    "__globals__", "__builtins__", "__import__", "__loader__", "__spec__",
    "__code__", "__func__", "__self__", "__closure__", "__dict__",
    "__module__", "__qualname__", "__getattribute__", "__getattr__",
    "__setattr__", "__delattr__", "__init_subclass__", "__class_getitem__",
    "__reduce__", "__reduce_ex__", "__sizeof__",
})


def check_ast_safety(code: str) -> list[str]:
    """
    Walk the AST of `code` and return a list of violation descriptions.
    Empty list means the code is safe to load.
    """
    violations: list[str] = []

    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return [f"Syntax error: {exc}"]

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root in _FORBIDDEN_MODULES:
                    violations.append(f"Forbidden import: {alias.name}")

        elif isinstance(node, ast.ImportFrom):
            if node.module:
                root = node.module.split(".")[0]
                if root in _FORBIDDEN_MODULES:
                    violations.append(f"Forbidden import: from {node.module}")

        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id in _FORBIDDEN_BUILTINS:
                violations.append(f"Forbidden call: {node.func.id}()")

        elif isinstance(node, ast.Attribute):
            # Catches both reads (x.__class__) and the LHS of writes
            if node.attr in _FORBIDDEN_ATTRS:
                violations.append(f"Forbidden attribute access: .{node.attr}")
            # Catch any other dunder we didn't list explicitly
            elif node.attr.startswith("__") and node.attr.endswith("__"):
                violations.append(f"Forbidden dunder attribute access: .{node.attr}")

        elif isinstance(node, ast.Name):
            # Catch references to forbidden names even outside calls
            if node.id in {"__builtins__", "__import__"}:
                violations.append(f"Forbidden name reference: {node.id}")

    return violations


# ── Immutability tags ─────────────────────────────────────────────────────────
#
# rules.py items can be tagged with:
#   QUORUM = 3  # #immutable          ← inline comment on the same line
#   PASSING_THRESHOLD = 0.5  # #immutable
#
#   # #immutable                       ← standalone comment on the line before
#   def tally_vote(...):
#
# Rules:
#   • #immutable content cannot be modified
#   • #immutable tags cannot be removed or downgraded to #mutable
#   • #mutable tags CAN be upgraded to #immutable (ratchet: protect but never unprotect)
#   • Untagged items are freely patchable

_INLINE_TAG_RE = re.compile(r'#\s*#(immutable|mutable)\b')
_STANDALONE_TAG_RE = re.compile(r'^\s*#\s*#(immutable|mutable)\b\s*$')
_DEF_RE = re.compile(r'^(async\s+)?def\s+(\w+)')
_CLASS_RE = re.compile(r'^class\s+(\w+)')
_ASSIGN_RE = re.compile(r'^(\w+)\s*(?::[^=]+)?\s*=(?!=)')  # name = ... but not ==


def extract_tagged_names(source: str) -> dict[str, str]:
    """
    Parse rules.py source and return {name: 'immutable'|'mutable'}
    for every tagged top-level definition or assignment.

    Supports two tag placements:
      1. Standalone comment on the line immediately before a def/class/assignment:
             # #immutable
             def tally_vote(...):
      2. Inline comment on the same line:
             QUORUM = 3  # #immutable
    """
    tags: dict[str, str] = {}
    lines = source.splitlines()
    pending_tag: str | None = None

    for line in lines:
        # Standalone tag line — remember it for the next definition
        m = _STANDALONE_TAG_RE.match(line)
        if m:
            pending_tag = m.group(1)
            continue

        # Try to identify what is being defined on this line
        stripped = line.lstrip()
        name: str | None = None

        def_m = _DEF_RE.match(stripped)
        class_m = _CLASS_RE.match(stripped)
        assign_m = _ASSIGN_RE.match(line)  # assignments must start at column 0

        if def_m:
            name = def_m.group(2)
        elif class_m:
            name = class_m.group(1)
        elif assign_m and not stripped.startswith(("#", " ", "\t")):
            name = assign_m.group(1)

        if name:
            inline = _INLINE_TAG_RE.search(line)
            tag = inline.group(1) if inline else pending_tag
            if tag:
                tags[name] = tag

        # Pending tag only ever applies to the line immediately following it
        pending_tag = None

    return tags


def extract_definition_ast(source: str) -> dict[str, str]:
    """
    Return {name: normalised_ast_repr} for every top-level definition.
    Uses ast.unparse so whitespace and comment differences are ignored —
    only semantic content changes matter.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return {}

    result: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            result[node.name] = ast.unparse(node)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    result[target.id] = ast.unparse(node.value)
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and node.value:
                result[node.target.id] = ast.unparse(node.value)

    return result


def check_immutable_violations(original: str, patched: str) -> list[str]:
    """
    Compare original and patched rules.py for BLOCKING immutability violations.
    Returns a list of human-readable violation strings (empty = no blocker).

    Blocking violations:
      - An #immutable item's content was changed (transmute first, then amend)
      - An #immutable tag was removed entirely (must be an explicit downgrade)
      - A patch both transmutes and modifies the same item (split into two)

    NOT blocking (allowed as a "transmutation" — see extract_transmutations):
      - An #immutable tag was downgraded to #mutable, with content unchanged
    """
    violations: list[str] = []

    orig_tags = extract_tagged_names(original)
    patched_tags = extract_tagged_names(patched)
    orig_defs = extract_definition_ast(original)
    patched_defs = extract_definition_ast(patched)

    for name, tag in orig_tags.items():
        if tag != "immutable":
            continue

        new_tag = patched_tags.get(name)

        if new_tag is None:
            violations.append(
                f"`{name}` is #immutable — tag cannot be silently removed "
                "(explicitly downgrade to #mutable to transmute)"
            )
            continue

        orig_body = orig_defs.get(name)
        patched_body = patched_defs.get(name)
        content_changed = orig_body is not None and orig_body != patched_body

        if new_tag == "mutable":
            # Transmutation — allowed, but must not change content in the same patch
            if content_changed:
                violations.append(
                    f"`{name}` is #immutable — cannot transmute and modify in the same patch "
                    "(submit transmutation first, then a follow-up amendment)"
                )
            continue

        # Still immutable — content must match
        if content_changed:
            violations.append(
                f"`{name}` is #immutable — content cannot be modified (transmute first)"
            )

    return violations


def extract_transmutations(original: str, patched: str) -> list[str]:
    """Return names of items being transmuted (#immutable -> #mutable) in this patch."""
    orig_tags = extract_tagged_names(original)
    patched_tags = extract_tagged_names(patched)
    return [
        name for name, tag in orig_tags.items()
        if tag == "immutable" and patched_tags.get(name) == "mutable"
    ]


# ── Patch validation ──────────────────────────────────────────────────────────

def _parse_patch_targets(patch_text: str) -> list[str]:
    """
    Extract target filenames from a unified diff.
    Returns the list of files the patch would modify/create/delete.
    """
    targets: list[str] = []
    for line in patch_text.splitlines():
        if line.startswith("+++ "):
            path = line[4:].split("\t")[0].strip()
            # Strip a/ or b/ git prefixes
            for prefix in ("b/", "a/"):
                if path.startswith(prefix):
                    path = path[len(prefix):]
                    break
            if path not in ("/dev/null", ""):
                targets.append(path)
    return targets


def _apply_patch_to_content(patch_text: str, current_content: str) -> tuple[bool, str, str]:
    """
    Apply patch_text to current_content in a temp dir.
    Returns (success, new_content, error_message).
    Tries -p1 (git style) then -p0 (plain style).
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        rules_file = tmp / "rules.py"
        patch_file = tmp / "change.patch"

        patch_file.write_text(patch_text, encoding="utf-8")

        for strip in (1, 0):
            rules_file.write_text(current_content, encoding="utf-8")
            result = subprocess.run(
                ["patch", f"-p{strip}", "--input", str(patch_file), str(rules_file)],
                capture_output=True,
                text=True,
            )
            # Clean up .orig that patch leaves behind
            orig = tmp / "rules.py.orig"
            if orig.exists():
                orig.unlink()

            if result.returncode == 0:
                return True, rules_file.read_text(encoding="utf-8"), ""

        return False, "", result.stderr.strip()


def validate_patch(
    patch_text: str, rules_path: Path
) -> tuple[bool, str, str | None, list[str]]:
    """
    Full validation pipeline.

    Returns:
        (True, "", new_rules_content, transmutations)  — patch is safe and applies
        (False, error_message, None, [])               — patch is rejected

    `transmutations` is the list of item names being downgraded from #immutable
    to #mutable. A non-empty list means the proposal is a transmutation and
    must clear the transmutation threshold to pass.
    """
    if not patch_text.strip():
        return False, "Patch is empty.", None, []

    # 1. Check which files the patch touches
    targets = _parse_patch_targets(patch_text)
    if not targets:
        return False, "Could not detect target files in patch.", None, []

    bad = [t for t in targets if t != "rules.py"]
    if bad:
        return False, f"Patch touches forbidden file(s): {', '.join(bad)}", None, []

    # 2. Apply to a temp copy
    current = rules_path.read_text(encoding="utf-8")
    ok, new_content, err = _apply_patch_to_content(patch_text, current)
    if not ok:
        return False, f"Patch does not apply cleanly:\n{err}", None, []

    # 3. AST safety check
    violations = check_ast_safety(new_content)
    if violations:
        bullet = "\n".join(f"• {v}" for v in violations)
        return False, f"Safety violations in patched rules.py:\n{bullet}", None, []

    # 4. Immutability check (blocking only — transmutations are allowed)
    immutable_violations = check_immutable_violations(current, new_content)
    if immutable_violations:
        bullet = "\n".join(f"• {v}" for v in immutable_violations)
        return False, f"Immutability violations:\n{bullet}", None, []

    # 5. Detect transmutations (allowed, but flagged for stricter tally)
    transmutations = extract_transmutations(current, new_content)

    return True, "", new_content, transmutations


# ── Patch application ─────────────────────────────────────────────────────────

def apply_patch(patch_text: str, rules_path: Path, proposal_id: int) -> tuple[bool, str]:
    """
    Apply a pre-validated patch to the live rules.py and git-commit the result.
    Returns (success, error_message).
    """
    current = rules_path.read_text(encoding="utf-8")
    ok, new_content, err = _apply_patch_to_content(patch_text, current)
    if not ok:
        return False, f"Patch failed on live file: {err}"

    rules_path.write_text(new_content, encoding="utf-8")

    # Attempt a git commit — non-fatal if git isn't configured
    try:
        rules_dir = rules_path.parent
        git_env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "Nomic Bot",
            "GIT_AUTHOR_EMAIL": "nomic-bot@localhost",
            "GIT_COMMITTER_NAME": "Nomic Bot",
            "GIT_COMMITTER_EMAIL": "nomic-bot@localhost",
        }
        subprocess.run(["git", "add", "rules.py"], cwd=rules_dir, capture_output=True, check=True)
        subprocess.run(
            ["git", "commit", "-m", f"Proposal #{proposal_id} passed"],
            cwd=rules_dir,
            capture_output=True,
            env=git_env,
            check=True,
        )
        log.info("Committed proposal #%d to rules repo.", proposal_id)
    except Exception as exc:
        log.warning("git commit failed (non-fatal): %s", exc)

    return True, ""


# ── Base-rules reset ──────────────────────────────────────────────────────────

def reset_rules_to_base(rules_path: Path) -> tuple[bool, str]:
    """Discard any in-game commits in the rules repo and snap back to
    origin/main. Used by /newgame so each new game starts from the
    canonical base rules rather than carrying forward the previous game's
    accepted proposals.

    Returns (True, "") on success, (False, error_message) on failure.
    """
    rules_dir = rules_path.parent
    try:
        fetch = subprocess.run(
            ["git", "fetch", "origin", "main"],
            cwd=rules_dir, capture_output=True, text=True, timeout=30,
        )
        if fetch.returncode != 0:
            return False, f"git fetch failed: {fetch.stderr.strip()}"

        reset = subprocess.run(
            ["git", "reset", "--hard", "origin/main"],
            cwd=rules_dir, capture_output=True, text=True, timeout=10,
        )
        if reset.returncode != 0:
            return False, f"git reset failed: {reset.stderr.strip()}"

        log.info("Reset rules.py to origin/main: %s", reset.stdout.strip())
        return True, ""
    except subprocess.TimeoutExpired as exc:
        return False, f"git operation timed out: {exc}"
    except Exception as exc:
        return False, f"reset failed: {exc}"


# ── Rule loading ──────────────────────────────────────────────────────────────

def load_rules(rules_path: Path) -> types.ModuleType:
    """Dynamically load rules.py as a module."""
    spec = importlib.util.spec_from_file_location("nomic_rules", rules_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def get_rule(rules: types.ModuleType, name: str, default):
    """Get a constant from rules, falling back to default."""
    return getattr(rules, name, default)


def call_rule(rules: types.ModuleType, func_name: str, *args, default=None):
    """
    Call a function from rules.py, returning `default` (or calling it if
    callable) if the function doesn't exist or raises.
    """
    func = getattr(rules, func_name, None)
    if func is None:
        return default() if callable(default) else default
    try:
        return func(*args)
    except Exception as exc:
        log.error("rules.%s(%s) raised: %s", func_name, args, exc)
        return default() if callable(default) else default


# ── Engine-enforced floors ────────────────────────────────────────────────────
#
# These are TRULY immutable — they live in engine code, not rules.py, so no
# proposal can ever weaken them. Mutable rules can only make things stricter.

ENGINE_QUORUM_FLOOR = 1  # lowered from 2 so 2-player games (where proposer
                         # can't vote) can still pass on a single YES
ENGINE_THRESHOLD_FLOOR = 0.5  # YES fraction (of valid votes) required


def safe_tally_vote(
    rules: types.ModuleType,
    yes: int,
    no: int,
    players: list,
    is_transmutation: bool,
) -> bool:
    """Tally a proposal with engine-enforced floors that rules.py cannot bypass.

    For both regular and transmutation proposals, the engine enforces:
      - Minimum total participation (ENGINE_QUORUM_FLOOR; full participation for transmutations)
      - Minimum YES fraction (ENGINE_THRESHOLD_FLOOR; 100% for transmutations)
      - yes > 0

    rules.tally_vote may then REJECT a proposal that would otherwise pass
    (e.g. by requiring a higher threshold), but cannot allow one that fails
    the engine floors.
    """
    total = yes + no

    if is_transmutation:
        required = max(1, len(players) - 1)  # all non-proposer players must vote
        if total < required or no > 0 or yes <= 0:
            return False
    else:
        if total < ENGINE_QUORUM_FLOOR or yes <= 0:
            return False
        if (yes / total) < ENGINE_THRESHOLD_FLOOR:
            return False

    # Floors passed — let rules.py impose any additional restrictions.
    # Default True: if rules.tally_vote is missing/broken, engine floors stand.
    return bool(call_rule(
        rules, "tally_vote", yes, no, players, is_transmutation,
        default=True,
    ))


def is_patch_valid(
    rules: types.ModuleType,
    patch_text: str,
    description: str,
    proposer_id: str,
    players: list,
) -> tuple[bool, str]:
    """Run rules.is_valid_patch (mutable, player-defined) on top of the engine's
    mandatory checks. Returns (True, "") on accept, (False, msg) on reject.

    A True return means accept. A string return rejects with that string as
    the reason. Anything else falsy rejects with a generic message.
    """
    result = call_rule(
        rules, "is_valid_patch",
        patch_text, description, proposer_id, players,
        default=True,
    )
    if result is True:
        return True, ""
    if isinstance(result, str) and result:
        return False, result
    return False, "Rejected by rules.is_valid_patch"


def safe_next_player(
    rules: types.ModuleType,
    current_id: str | None,
    players: list,
) -> str | None:
    """Resolve the next player to take a turn, with validation.

    If rules.next_player returns an ID that isn't in the roster (or any other
    nonsense), the engine falls back to deterministic round-robin so a bad
    rule can't freeze the game.
    """
    ids = [p["discord_id"] for p in players]
    if not ids:
        return None

    def round_robin() -> str:
        if current_id not in ids:
            return ids[0]
        idx = ids.index(current_id)
        return ids[(idx + 1) % len(ids)]

    candidate = call_rule(rules, "next_player", current_id, players, default=round_robin)
    if isinstance(candidate, str) and candidate in ids:
        return candidate
    log.warning("rules.next_player returned %r (not in roster); falling back to round-robin", candidate)
    return round_robin()
