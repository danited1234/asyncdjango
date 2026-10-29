"""Help port an ADRF code base: ``python -m asyncdjango.port [PATH ...] [--write]``.

Rewrites ``adrf`` imports to ``asyncdjango.rest_framework`` and the two
mechanical ``sync_to_async`` patterns::

    await sync_to_async(serializer.is_valid)(...)  ->  await serializer.ais_valid(...)
    await sync_to_async(lambda: serializer.data)()  ->  await serializer.adata

and lists every other ``sync_to_async`` call by what the wrapped code appears
to do: blocking I/O (keep it), database only (candidate for
``sync_to_native``), or unknown. The sort is a heuristic over names in the
wrapped code; read each candidate before switching it.

Without ``--write`` nothing is changed and the rewrites are listed instead.
"""

from __future__ import annotations

import argparse
import ast
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

ADRF_MODULES = {"decorators", "fields", "generics", "mixins", "requests", "routers",
                "serializers", "shortcuts", "views", "viewsets"}
SKIP_DIRS = {".git", ".venv", "venv", "env", "node_modules", "__pycache__", "migrations",
             "site-packages"}
OFFLOADERS = {"sync_to_async", "database_sync_to_async"}

# Names whose presence in wrapped code means a blocking call that must stay
# on a thread. Matched against every name and attribute in the code.
BLOCKING = {
    "requests", "urlopen", "urllib3", "boto3", "botocore", "stripe", "smtplib", "send_mail",
    "send_mass_mail", "mail_admins", "EmailMessage", "EmailMultiAlternatives", "delay",
    "apply_async", "make_password", "check_password", "set_password", "authenticate",
    "open", "sleep", "subprocess", "weasyprint", "write_pdf", "HTML", "render_to_string",
    "storage", "default_storage", "upload_file", "put_object", "Redis", "cache", "send",
}
# Fragments of called functions' names that suggest the same (generate_invoice_pdf).
BLOCKING_PARTS = ("pdf", "mail", "stripe", "http", "s3", "upload", "download", "webhook",
                  "sms", "twilio", "slack")
# Names that point at ORM work.
ORM = {
    "objects", "save", "delete", "refresh_from_db", "create", "get_or_create",
    "update_or_create", "bulk_create", "bulk_update", "filter", "exclude", "get", "first",
    "last", "count", "exists", "aggregate", "annotate", "values", "values_list",
    "select_related", "prefetch_related", "order_by", "update", "atomic", "data",
    "is_valid", "list", "all", "set", "add", "remove", "clear",
}


@dataclass
class Finding:
    path: Path
    line: int
    kind: str  # a key of LABELS
    text: str


def _names(node: ast.AST) -> set[str]:
    names = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Name):
            names.add(child.id)
        elif isinstance(child, ast.Attribute):
            names.add(child.attr)
    return names


def _called(node: ast.AST) -> set[str]:
    names = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            func = child.func
            names.add(func.id if isinstance(func, ast.Name) else getattr(func, "attr", ""))
    return names


class _Scanner:
    def __init__(self, path: Path, source: str) -> None:
        self.path = path
        self.source = source
        self.lines = source.encode().splitlines(keepends=True)
        self.tree = ast.parse(source, filename=str(path))
        self.functions = {node.name: node for node in ast.walk(self.tree)
                          if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)}
        self.findings: list[Finding] = []
        # (start offset, end offset, replacement), in UTF-8 bytes.
        self.edits: list[tuple[int, int, str]] = []

    def offset(self, line: int, col: int) -> int:
        return sum(len(text) for text in self.lines[:line - 1]) + col

    def segment(self, node: ast.AST) -> str:
        return ast.get_source_segment(self.source, node) or ""

    def replace(self, node: ast.AST, text: str) -> None:
        self.edits.append((self.offset(node.lineno, node.col_offset),
                           self.offset(node.end_lineno, node.end_col_offset), text))
        self.findings.append(Finding(self.path, node.lineno, "rewrite",
                                     f"{self.segment(node)}  ->  {text}"))

    def scan(self) -> None:
        for node in ast.walk(self.tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                self.imports(node, node.module)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.asname is None and alias.name.startswith("adrf"):
                        # Code refers to it as adrf.x; rename those uses by hand.
                        self.findings.append(Finding(self.path, node.lineno, "manual",
                                                     self.segment(node)))
                    else:
                        self.imports(node, alias.name)
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Call):
                self.offload_call(node, node.func)
            elif isinstance(node, ast.FunctionDef):
                if any(self.is_offloader(decorator) for decorator in node.decorator_list):
                    self.classify(node, node.body, f"@sync_to_async def {node.name}")

    def imports(self, node: ast.Import | ast.ImportFrom, module: str) -> None:
        if module != "adrf" and not module.startswith("adrf."):
            return
        submodule = module.partition(".")[2].partition(".")[0]
        if submodule and submodule not in ADRF_MODULES:
            self.findings.append(Finding(self.path, node.lineno, "no-equivalent",
                                         self.segment(node)))
            return
        old = self.segment(node)
        self.replace(node, old.replace(module, "asyncdjango.rest_framework" + module[4:], 1))

    def is_offloader(self, node: ast.AST) -> bool:
        if isinstance(node, ast.Call):
            node = node.func
        name = node.id if isinstance(node, ast.Name) else getattr(node, "attr", None)
        return name in OFFLOADERS

    def offload_call(self, outer: ast.Call, inner: ast.Call) -> None:
        if not self.is_offloader(inner.func) or len(inner.args) != 1:
            return
        target = inner.args[0]
        call_args = self.source_args(outer)
        if isinstance(target, ast.Attribute) and target.attr == "is_valid" and not inner.keywords:
            self.replace(outer, f"{self.segment(target.value)}.ais_valid({call_args})")
        elif (isinstance(target, ast.Lambda) and not target.args.args and not outer.args
              and isinstance(target.body, ast.Attribute) and target.body.attr == "data"):
            self.replace(outer, f"{self.segment(target.body.value)}.adata")
        elif isinstance(target, ast.Lambda):
            self.classify(outer, [target.body], self.segment(outer))
        else:
            function = self.functions.get(getattr(target, "id", None))
            body = function.body if function is not None else []
            self.classify(outer, body, self.segment(outer), callee=target)

    def source_args(self, call: ast.Call) -> str:
        parts = [self.segment(arg) for arg in call.args]
        parts += [self.segment(keyword) for keyword in call.keywords]
        return ", ".join(parts)

    def classify(self, node: ast.AST, body: list[ast.AST], text: str,
                 callee: ast.AST | None = None) -> None:
        if callee is not None:  # sync_to_async(generate_pdf)(...)
            body = [*body, callee]
        names = set().union(*(_names(part) for part in body))
        called = set().union(*(_called(part) for part in body))
        if callee is not None:
            called |= _names(callee)
        blocking = names & BLOCKING | {
            name for name in called if any(part in name.lower() for part in BLOCKING_PARTS)}
        if blocking:
            kind = "blocking"
            text += f"  [{', '.join(sorted(blocking))}]"
        elif names & ORM:
            kind = "orm"
        else:
            kind = "unknown"
        self.findings.append(Finding(self.path, node.lineno, kind, " ".join(text.split())))

    def rewritten(self) -> str:
        data = b"".join(self.lines)
        for start, end, text in sorted(self.edits, reverse=True):
            data = data[:start] + text.encode() + data[end:]
        return data.decode()


def python_files(paths: list[Path]) -> Iterator[Path]:
    for path in paths:
        if path.is_file():
            yield path
            continue
        for child in sorted(path.rglob("*.py")):
            if not SKIP_DIRS.intersection(child.relative_to(path).parts[:-1]):
                yield child


LABELS = {
    "rewrite": "Rewritten",
    "no-equivalent": "adrf modules without an asyncdjango equivalent",
    "manual": "Plain `import adrf...`: change the import and its uses by hand",
    "orm": "Database only: candidates for sync_to_native",
    "blocking": "Blocking I/O: keep sync_to_async",
    "unknown": "Unknown: read the wrapped code",
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m asyncdjango.port",
                                     description=__doc__.split("\n\n")[0])
    parser.add_argument("paths", nargs="*", type=Path, default=[Path(".")])
    parser.add_argument("--write", action="store_true", help="apply the rewrites")
    args = parser.parse_args(argv)

    findings: list[Finding] = []
    for path in python_files(args.paths):
        source = path.read_bytes().decode()
        try:
            scanner = _Scanner(path, source)
        except SyntaxError as exc:
            print(f"{path}: skipped ({exc.msg})", file=sys.stderr)
            continue
        scanner.scan()
        findings.extend(scanner.findings)
        if args.write and scanner.edits:
            path.write_bytes(scanner.rewritten().encode())

    for kind, label in LABELS.items():
        group = sorted((f for f in findings if f.kind == kind), key=lambda f: (f.path, f.line))
        if not group:
            continue
        if kind == "rewrite" and not args.write:
            label = "To rewrite (run with --write)"
        print(f"\n{label} ({len(group)}):")
        for finding in group:
            print(f"  {finding.path}:{finding.line}: {finding.text}")
    if not findings:
        print("Nothing to port.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
