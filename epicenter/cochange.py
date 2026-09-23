"""
Change coupling: which symbols keep getting edited together.

Two functions that always change in the same commit are coupled even when
neither calls the other — a config key and the code reading it, an encoder and
its decoder, a model and the migration that shapes it. Structural analysis
cannot see any of that, because there is no edge to find. History can.

Analysis is symbol-level rather than file-level: each commit's diff hunks are
mapped onto the symbols defined in that file *at that commit*, so a change to
one method in a large module does not implicate the rest of it.
"""

import ast
import os
import subprocess
from collections import Counter, defaultdict

from .database import save_cochange_to_db
from .pipeline import (
    DefinitionVisitor,
    module_name_for,
    package_prefix_for,
    src_layout_dirs,
    strip_src_layout,
)

DEFAULT_MAX_COMMITS = 500

# Commits touching a large share of the codebase say nothing about coupling —
# a reformat, a licence header, a mass rename. Including them would couple
# every symbol to every other.
MAX_FILES_PER_COMMIT = 25


def _git(repo_root, *args):
    result = subprocess.run(
        ["git", "-C", repo_root, *args],
        capture_output=True, text=True, errors="replace",
    )
    if result.returncode != 0:
        return None
    return result.stdout


def repository_root(path):
    out = _git(path, "rev-parse", "--show-toplevel")
    return out.strip() if out else None


def tracked_python_files(repo_root, realm):
    """
    Python files under `realm` that this repository actually tracks.

    The check matters: a realm can sit inside an unrelated repository — a
    library under .venv, say — where git walks up and happily reports the
    enclosing project's history. Tracked files are what tie the two together.
    """
    rel = os.path.relpath(realm, repo_root)
    prefix = "" if rel == "." else rel + os.sep
    out = _git(repo_root, "ls-files", "--", os.path.join(rel, "*.py"))
    if not out:
        return set()
    return {
        line for line in out.splitlines()
        if line.endswith(".py") and (not prefix or line.startswith(prefix))
    }


def changed_line_ranges(repo_root, sha, path):
    """Line ranges touched in the post-image of `path` at `sha`."""
    out = _git(
        repo_root, "show", "--format=", "--unified=0",
        "--no-color", sha, "--", path,
    )
    if not out:
        return []

    ranges = []
    for line in out.splitlines():
        if not line.startswith("@@"):
            continue
        try:
            after = line.split("+", 1)[1].split("@@", 1)[0].strip()
        except IndexError:
            continue
        start, _, count = after.partition(",")
        try:
            start = int(start)
            count = int(count) if count else 1
        except ValueError:
            continue
        if count == 0:
            # A pure deletion has no post-image lines; attribute it to the
            # symbol surrounding the point the lines were removed from.
            ranges.append((max(start, 1), max(start, 1)))
        else:
            ranges.append((start, start + count - 1))
    return ranges


def symbol_spans(source, module_name, is_package):
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return {}
    visitor = DefinitionVisitor(module_name, is_package)
    visitor.visit(tree)
    return visitor.spans


def symbols_touched(repo_root, sha, path, module_name, is_package):
    """
    Symbols whose definition overlaps a changed line range.

    The file is read at `sha`, not from the working tree: line numbers in a
    historical diff refer to the file as it was then, and mapping them onto
    today's layout would attribute changes to whatever happens to sit at those
    lines now.
    """
    ranges = changed_line_ranges(repo_root, sha, path)
    if not ranges:
        return set()

    source = _git(repo_root, "show", f"{sha}:{path}")
    if source is None:
        return set()

    spans = symbol_spans(source, module_name, is_package)

    touched = set()
    for lo, hi in ranges:
        overlapping = {
            symbol: span for symbol, span in spans.items()
            if not (span[1] < lo or span[0] > hi)
        }
        if not overlapping:
            # Module-level code: imports, constants, configuration. Real
            # changes that belong to the module itself, and a frequent half of
            # exactly the coupling this is meant to find — a config key and
            # whatever reads it.
            touched.add(module_name)
            continue

        # Attribute the change to the innermost symbol covering it. A class
        # span encloses its methods, so counting every enclosing symbol would
        # couple each class to its own methods in every commit and drown out
        # the coupling actually worth seeing.
        for symbol, (start, end) in overlapping.items():
            encloses_another = any(
                other != symbol and start <= ostart and oend <= end
                for other, (ostart, oend) in overlapping.items()
            )
            if not encloses_another:
                touched.add(symbol)

    return touched


def analyze_history(realm, max_commits=DEFAULT_MAX_COMMITS, progress=None):
    """
    Walks recent history and counts which symbols change together.

    Returns (pair_counts, symbol_counts, commits_examined), or None when the
    realm has no usable history.
    """
    # git reports its toplevel with symlinks resolved, so the realm has to be
    # resolved the same way before the two paths can be compared. On macOS
    # /var is a symlink to /private/var, which is enough to make every
    # relative path come out wrong and every file look untracked.
    realm = os.path.realpath(realm)
    root = repository_root(realm)
    if not root:
        return None
    root = os.path.realpath(root)

    tracked = tracked_python_files(root, realm)
    if not tracked:
        return None

    package_prefix = package_prefix_for(realm)
    src_dirs = src_layout_dirs(realm)
    rel_realm = os.path.relpath(realm, root)

    log = _git(
        root, "log", "--no-merges", f"--max-count={max_commits}",
        "--format=%H", "--", rel_realm,
    )
    if not log:
        return None

    pairs = Counter()
    symbol_counts = Counter()
    examined = 0

    for sha in log.split():
        # --root so the initial commit, which has no parent to diff against,
        # still reports the files it introduced.
        listing = _git(
            root, "diff-tree", "--root", "--no-commit-id", "--name-only", "-r", sha
        )
        if not listing:
            continue

        files = [f for f in listing.splitlines() if f in tracked]
        if not files or len(files) > MAX_FILES_PER_COMMIT:
            continue

        touched = set()
        for path in files:
            rel_to_realm = os.path.relpath(path, rel_realm) if rel_realm != "." else path
            rel_to_realm = strip_src_layout(rel_to_realm, src_dirs)
            module_name, is_package = module_name_for(rel_to_realm, package_prefix)
            touched |= symbols_touched(root, sha, path, module_name, is_package)

        if not touched:
            continue

        examined += 1
        for symbol in touched:
            symbol_counts[symbol] += 1

        ordered = sorted(touched)
        for i, a in enumerate(ordered):
            for b in ordered[i + 1:]:
                pairs[(a, b)] += 1

        if progress:
            progress(examined)

    return pairs, symbol_counts, examined


def ingest_history(realm, db_path="epicenter.db", max_commits=DEFAULT_MAX_COMMITS):
    """Analyses history and stores it. Returns a summary dict, or None."""
    result = analyze_history(realm, max_commits=max_commits)
    if result is None:
        return None

    pairs, symbol_counts, examined = result
    save_cochange_to_db(os.path.abspath(realm), pairs, symbol_counts, db_path)
    return {
        "commits": examined,
        "symbols": len(symbol_counts),
        "pairs": len(pairs),
    }
