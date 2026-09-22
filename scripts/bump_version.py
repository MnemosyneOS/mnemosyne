#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Mnemosyne OS — release version bump
===================================

Rewrites the *declared* version of the tree, with an explicit rule list instead of
a blind find-and-replace.

Why not a blind replace?  ``v7.0.2`` also appears in roughly 150 code comments
that record *when a fix landed* — ``# v7.0.2 (P0-1): access_count 上限``. Those
comments are history and staying accurate is the point of them; rewriting them to
``v8.0.0`` would falsify the record. A blind replace would also corrupt any string
that happens to contain a version-like substring (the previous release's script
had to guard ``127.0.0.1`` for exactly this reason).

So this script applies a fixed list of *declaration shapes*, and reports how many
substitutions each rule made. A rule that matches nothing is reported too, because
a silently unused rule usually means a declaration was moved or renamed and is now
drifting.

Usage::

    python scripts/bump_version.py --from 7.0.2 --to 8.0.0 --dry-run
    python scripts/bump_version.py --from 7.0.2 --to 8.0.0 --apply
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from typing import List, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: Directories never walked.
SKIP_DIRS = frozenset({".git", "__pycache__", ".pytest_cache", ".mypy_cache",
                       "node_modules", "dist", "build", ".venv", "venv"})

#: Files that are byte-identical history and must not be rewritten.
SKIP_FILES = frozenset({"CHANGELOG.md"})

#: Binary-ish extensions never read as text.
SKIP_EXT = frozenset({".png", ".jpg", ".jpeg", ".gif", ".ico", ".webp", ".pdf",
                      ".zip", ".gz", ".tar", ".whl", ".so", ".dll", ".exe",
                      ".woff", ".woff2", ".ttf", ".db", ".pyc", ".sqlite"})


def rules(old: str, new: str) -> List[Tuple[str, str, str]]:
    """Return ``(label, pattern, replacement)`` triples.

    Ordered most specific first so a broad rule cannot consume a line a narrow
    rule was meant to handle.
    """
    o = re.escape(old)
    n = new
    return [
        # ---- code declarations -------------------------------------------
        ("__version__ assignment",
         rf'(__version__\s*=\s*"){o}(")', rf"\g<1>{n}\g<2>"),
        ("VERSION assignment",
         rf'(^VERSION\s*=\s*"){o}(")', rf"\g<1>{n}\g<2>"),
        ("setup.py version=",
         rf'(version\s*=\s*"){o}(")', rf"\g<1>{n}\g<2>"),
        ("MCP serverInfo version",
         rf'("version"\s*:\s*"){o}(")', rf"\g<1>{n}\g<2>"),
        ("JSON version field",
         rf'("version"\s*:\s*"){o}(")', rf"\g<1>{n}\g<2>"),
        ("pyproject version",
         rf'(^version\s*=\s*"){o}(")', rf"\g<1>{n}\g<2>"),

        # ---- user-facing product strings ---------------------------------
        ("Mnemosyne OS v<ver>", rf"(Mnemosyne OS v){o}", rf"\g<1>{n}"),
        ("Mnemosyne OS <ver>", rf"(Mnemosyne OS ){o}\b", rf"\g<1>{n}"),
        ("Mnemosyne OS Engine v<ver>",
         rf"(Mnemosyne OS Engine v){o}", rf"\g<1>{n}"),
        ("Mnemosyne v<ver>", rf"(Mnemosyne v){o}\b", rf"\g<1>{n}"),
        ("Mnemosyne <ver>", rf"(Mnemosyne ){o}\b", rf"\g<1>{n}"),
        ("Mnemosyne<ver> (structure/paths)",
         rf"(Mnemosyne){o}(/|\b)", rf"\g<1>{n}\g<2>"),
        ("MnemosyneWeb/<ver> (User-Agent, server_version)",
         rf"(MnemosyneWeb/){o}", rf"\g<1>{n}"),
        ("<product> 管理端 v<ver>", rf"(管理端 v){o}", rf"\g<1>{n}"),
        ("v<ver> Stable", rf"(v){o}(\s+Stable)", rf"\g<1>{n}\g<2>"),

        # ---- documentation phrasings -------------------------------------
        # A version number in prose can appear either before or after the noun it
        # qualifies ("8.0.0 memory stack" / "память 7.0.2"), so both orders are
        # covered — in each of the nine languages the repository ships.
        ("<ver> <localised stack noun>",
         rf"{o}([\s\-]*(?:memory stack|记忆栈|記憶棧|メモリスタック|메모리\s*스택|"
         rf"หน่วยความจำ|Speicher-Stack|Speicher|pila\s+de\s+memoria|стека))",
         rf"{n}\g<1>"),
        ("确认装的是 <ver>", rf"(确认装的是 ){o}", rf"\g<1>{n}"),
        ("结合 <ver> 实际能力", rf"(结合 ){o}( 实际能力)", rf"\g<1>{n}\g<2>"),
        ("MCP 工具面（<n> 个，<ver>）",
         rf"(MCP 工具面（20 个，){o}(）)", rf"\g<1>{n}\g<2>"),
        ("memory stack <ver>", rf"(memory stack ){o}\b", rf"\g<1>{n}"),
        ("记忆栈 <ver>", rf"(记忆栈已?确证缺陷[^）)]*){o}", rf"\g<1>{n}"),
        ("记忆栈（<ver>）", rf"(记忆栈）?\s*){o}", rf"\g<1>{n}"),
        ("截至 <ver>", rf"(截至 ){o}", rf"\g<1>{n}"),
        ("当前版本（<ver>）", rf"(当前版本（){o}(）)", rf"\g<1>{n}\g<2>"),
        ("speicher-stack <ver>", rf"(Speicher-Stack:?[^.]*?){o}", rf"\g<1>{n}"),
        ("pila de memoria <ver>", rf"(pila de memoria ){o}", rf"\g<1>{n}"),
        ("스택 <ver>", rf"(스택[^:]*?){o}", rf"\g<1>{n}"),
        ("スタック <ver>", rf"(スタック[^：]*?){o}", rf"\g<1>{n}"),
        ("สแตก <ver>", rf"(สแตก[^ ]*?){o}", rf"\g<1>{n}"),
        ("стека <ver>", rf"(стека[^:]*?){o}", rf"\g<1>{n}"),
    ]


def iter_files(root: str) -> List[str]:
    out: List[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            if name in SKIP_FILES:
                continue
            if os.path.splitext(name)[1].lower() in SKIP_EXT:
                continue
            out.append(os.path.join(dirpath, name))
    return sorted(out)


def main() -> int:
    parser = argparse.ArgumentParser(description="Bump the declared release version")
    parser.add_argument("--from", dest="old", required=True)
    parser.add_argument("--to", dest="new", required=True)
    parser.add_argument("--apply", action="store_true",
                       help="write the changes (default is a dry run)")
    parser.add_argument("--root", default=ROOT)
    args = parser.parse_args()

    rule_list = rules(args.old, args.new)
    files = iter_files(args.root)
    per_rule = {label: 0 for label, _, _ in rule_list}
    changed: List[Tuple[str, int]] = []
    errors: List[str] = []

    for path in files:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                text = fh.read()
        except (UnicodeDecodeError, OSError) as e:
            errors.append(f"{path}: {e}")
            continue

        original = text
        count = 0
        for label, pattern, replacement in rule_list:
            text, hits = re.subn(pattern, replacement, text, flags=re.MULTILINE)
            if hits:
                per_rule[label] += hits
                count += hits

        if text != original:
            changed.append((os.path.relpath(path, args.root), count))
            if args.apply:
                with open(path, "w", encoding="utf-8", newline="") as fh:
                    fh.write(text)

    print(f"{'APPLIED' if args.apply else 'DRY RUN'}  "
          f"{args.old} -> {args.new}")
    print(f"files scanned : {len(files)}")
    print(f"files changed : {len(changed)}")
    print(f"substitutions : {sum(per_rule.values())}")
    print()
    print("per rule:")
    for label, hits in per_rule.items():
        mark = "" if hits else "   <- matched nothing; check the declaration still exists"
        print(f"  {hits:5d}  {label}{mark}")
    print()
    print("files:")
    for rel, count in changed:
        print(f"  {count:4d}  {rel}")
    if errors:
        print()
        print("skipped (undecodable):")
        for err in errors:
            print(f"  {err}")

    if not args.apply:
        print()
        print("re-run with --apply to write.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
