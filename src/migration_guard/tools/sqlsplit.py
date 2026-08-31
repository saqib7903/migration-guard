"""Minimal, dependency-free SQL statement splitter.

Handles single quotes, double-quoted identifiers, dollar-quoted bodies ($$ ... $$,
$tag$ ... $tag$), line comments and block comments. Good enough for migration files,
which is all we claim.
"""
from __future__ import annotations

import re


def strip_comments(sql: str) -> str:
    out = []
    i, n = 0, len(sql)
    while i < n:
        c = sql[i]
        if c == "-" and sql.startswith("--", i):
            j = sql.find("\n", i)
            i = n if j == -1 else j
            continue
        if c == "/" and sql.startswith("/*", i):
            j = sql.find("*/", i + 2)
            i = n if j == -1 else j + 2
            continue
        if c == "'":
            j = i + 1
            while j < n:
                if sql[j] == "'" and (j + 1 >= n or sql[j + 1] != "'"):
                    break
                if sql[j] == "'" and sql[j + 1] == "'":
                    j += 1
                j += 1
            out.append(sql[i : j + 1])
            i = j + 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


_DOLLAR = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)?\$")


def split_statements(sql: str) -> list[str]:
    sql = strip_comments(sql)
    stmts: list[str] = []
    buf: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        c = sql[i]
        if c == "'":
            j = i + 1
            while j < n:
                if sql[j] == "'":
                    if j + 1 < n and sql[j + 1] == "'":
                        j += 2
                        continue
                    break
                j += 1
            buf.append(sql[i : j + 1])
            i = j + 1
            continue
        if c == '"':
            j = sql.find('"', i + 1)
            j = n - 1 if j == -1 else j
            buf.append(sql[i : j + 1])
            i = j + 1
            continue
        if c == "$":
            m = _DOLLAR.match(sql, i)
            if m:
                tag = m.group(0)
                j = sql.find(tag, m.end())
                j = n if j == -1 else j + len(tag)
                buf.append(sql[i:j])
                i = j
                continue
        if c == ";":
            s = "".join(buf).strip()
            if s:
                stmts.append(s)
            buf = []
            i += 1
            continue
        buf.append(c)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        stmts.append(tail)
    return stmts


def normalize(stmt: str) -> str:
    """Collapse whitespace, uppercase-insensitive compare helper."""
    return re.sub(r"\s+", " ", stmt).strip()
