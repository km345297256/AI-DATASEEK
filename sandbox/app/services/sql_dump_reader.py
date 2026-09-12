"""Bounded SQL dump inspection. Never executes SQL or opens a database.

SQLGlot is used only for small CREATE TABLE ASTs. A single-pass lexer handles
statement boundaries and literal tuples; COPY text is decoded separately. All
nontrivial evaluation, missing-column defaults and ambiguous string modes are
rejected. Run only in the existing one-shot restricted worker.
"""
from __future__ import annotations

from dataclasses import dataclass
import logging
import re
import time

from .sql_dump_payload import (
    ERROR, LIMITS, MAX_INPUT, SqlDumpError, _TYPE, build_sql_dump_payload,
    name, need, safe_text, table_id, validate_sql_dump_options,
)

PARSER_VERSION = "30.18.0"
MAX_TOKENS = 524288
MAX_STATEMENT_TOKENS = 65536
MAX_SCHEMA_TOKENS = 8192
MAX_COPY_ROW_BYTES = 65536
DEADLINE_SECONDS = 15.0
_WORD = re.compile(r"[^\W\d][\w$]*", re.UNICODE)
_SPACE = re.compile(r"\s+")
_NUM = re.compile(r"(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")
_HEX = re.compile(r"0[xX][0-9a-fA-F]+")
_DOLLAR = re.compile(r"\$(?:[A-Za-z_][A-Za-z_0-9]{0,63})?\$")
_AST_ALLOWED = {
    "Create", "Schema", "Table", "ColumnDef", "Identifier", "DataType", "DataTypeParam", "Literal",
    "ColumnConstraint", "NotNullColumnConstraint", "PrimaryKeyColumnConstraint", "UniqueColumnConstraint",
    "DefaultColumnConstraint", "AutoIncrementColumnConstraint", "Null", "Boolean", "Neg",
    "PrimaryKey", "Unique", "Constraint", "IndexParameters", "Properties", "EngineProperty",
    "CharacterSetProperty", "CollateProperty", "AutoIncrementProperty", "Var", "RowFormatProperty",
}
_IGNORABLE = {"SET", "ALTER", "DROP", "PRAGMA", "BEGIN", "COMMIT", "ROLLBACK", "LOCK", "UNLOCK",
              "SELECT", "COMMENT", "GRANT", "REVOKE", "USE", "DO"}


@dataclass
class Token:
    kind: str
    value: str
    start: int
    end: int


class Cursor:
    def __init__(self, text, dialect):
        self.text, self.dialect, self.pos = text, dialect, 0
        self.tokens = 0
        self.deadline = time.monotonic() + DEADLINE_SECONDS

    def check(self): need(time.monotonic() <= self.deadline)

    def skip(self):
        text = self.text; end = len(text)
        while self.pos < end:
            self.check()
            if match := _SPACE.match(text, self.pos): self.pos = match.end(); continue
            dash_comment = text.startswith("--", self.pos) and (self.dialect != "mysql" or self.pos + 2 == end or text[self.pos + 2].isspace())
            if dash_comment or self.dialect == "mysql" and text[self.pos] == "#":
                stop = text.find("\n", self.pos)
                self.pos = end if stop < 0 else stop + 1; continue
            if text.startswith("/*", self.pos):
                level = 1; self.pos += 2
                while level:
                    need(self.pos < end); self.check()
                    a = text.find("/*", self.pos); b = text.find("*/", self.pos)
                    need(b >= 0)
                    if a >= 0 and a < b:
                        # Only PostgreSQL defines nested block comments. Reject
                        # nested comments in other dialects instead of guessing.
                        need(self.dialect == "postgres"); level += 1
                        need(level <= LIMITS["max_depth"]); self.pos = a + 2
                    else: level -= 1; self.pos = b + 2
                continue
            break

    def token(self):
        self.skip(); text = self.text; start = self.pos
        if start == len(text): return None
        self.tokens += 1; need(self.tokens <= MAX_TOKENS)
        self.check(); c = text[start]
        if c in "'\"`[":
            if c == "'": kind, close = "string", c
            else:
                need(c == '"' and self.dialect != "mysql" or c == "`" and self.dialect in {"sqlite", "mysql"} or c == "[" and self.dialect == "sqlite")
                kind, close = "identifier", "]" if c == "[" else c
            self.pos += 1; chunks = []; segment = self.pos
            while self.pos < len(text):
                need(self.pos - start <= LIMITS["max_statement_bytes"])
                current = text[self.pos]
                if current == close:
                    chunks.append(text[segment:self.pos]); self.pos += 1
                    if self.pos < len(text) and text[self.pos] == close and close != "]":
                        chunks.append(close); self.pos += 1; segment = self.pos; continue
                    value = "".join(chunks)
                    # MySQL/PG ordinary backslashes depend on session settings.
                    # Never silently choose a SQL mode or mutate a SET state.
                    need(kind != "string" or self.dialect == "sqlite" or "\\" not in value)
                    return Token(kind, value, start, self.pos)
                self.pos += 1
                if self.pos % 4096 == 0: self.check()
            need(False)
        if c == "$" and self.dialect == "postgres":
            match = _DOLLAR.match(text, start); need(match is not None)
            stop = text.find(match.group(), match.end()); need(stop >= 0)
            self.pos = stop + len(match.group())
            need(self.pos - start <= LIMITS["max_statement_bytes"])
            return Token("dollar", text[match.end():stop], start, self.pos)
        if self.dialect == "mysql" and (match := _HEX.match(text, start)):
            self.pos = match.end(); return Token("hex", match.group()[2:], start, self.pos)
        if match := _NUM.match(text, start):
            self.pos = match.end(); return Token("number", match.group(), start, self.pos)
        if match := _WORD.match(text, start):
            self.pos = match.end(); return Token("word", match.group(), start, self.pos)
        self.pos += 1
        return Token(c, c, start, self.pos)

    def statement(self):
        self.skip()
        if self.pos == len(self.text): return None
        if self.text[self.pos] == "\\":
            # psql controls are inert, not executed. COPY's terminator is only
            # legal inside its data block and is handled by copy_rows.
            need(self.dialect == "postgres" and not self.text.startswith("\\.", self.pos))
            end = self.text.find("\n", self.pos)
            stop = len(self.text) if end < 0 else end + 1
            need(stop - self.pos <= LIMITS["max_statement_bytes"])
            self.pos = stop; return [], None
        result = []; start = self.pos; depth = 0
        while True:
            token = self.token()
            need(token is not None)  # truncated SQL must not appear complete
            need(self.pos - start <= LIMITS["max_statement_bytes"])
            if token.kind == ";":
                need(depth == 0)
                raw = self.text[start:token.start]
                need(len(raw.encode("utf8")) <= LIMITS["max_statement_bytes"])
                return result, raw
            if token.kind == "(": depth += 1; need(depth <= LIMITS["max_depth"])
            elif token.kind == ")": depth -= 1; need(depth >= 0)
            result.append(token); need(len(result) <= MAX_STATEMENT_TOKENS)


class Tokens:
    def __init__(self, values, dialect): self.values, self.pos, self.dialect = values, 0, dialect
    def done(self): return self.pos == len(self.values)
    def peek(self): return None if self.done() else self.values[self.pos]
    def pop(self): need(not self.done()); token = self.values[self.pos]; self.pos += 1; return token
    def is_word(self, word): return self.peek() is not None and self.peek().kind == "word" and self.peek().value.upper() == word
    def word(self, word): need(self.is_word(word)); self.pop()
    def symbol(self, sym): need(self.pop().kind == sym)
    def identifier(self):
        token = self.pop(); need(token.kind in {"identifier", "word"} and name(token.value))
        return token.value.lower() if token.kind == "word" and self.dialect == "postgres" else token.value
    def table(self):
        label = self.identifier()
        if self.peek() is not None and self.peek().kind == ".":
            self.pop(); label += "." + self.identifier()
        need(name(label)); return label
    def columns(self):
        self.symbol("("); result = [self.identifier()]
        while self.peek() is not None and self.peek().kind == ",":
            self.pop(); result.append(self.identifier()); need(len(result) <= 128)
        self.symbol(")"); need(len(set(x.casefold() for x in result)) == len(result)); return result


def _text(value):
    size = len(value.encode("utf8", errors="strict"))
    if size > 512 or not safe_text(value):
        return {"type": "text-omitted", "bytes": size, "reason": "cell-budget" if size > 512 else "unsafe-text"}
    return {"type": "text", "value": value}


def _literal(tokens):
    token = tokens.pop()
    if token.kind in {"string", "dollar"}: return _text(token.value)
    if token.kind == "word":
        if token.value.upper() == "NULL": return {"type": "null", "value": None}
        if token.value.upper() in {"TRUE", "FALSE"}: return {"type": "boolean", "value": token.value.upper() == "TRUE"}
        if token.value.upper() == "X" and tokens.dialect in {"sqlite", "mysql"}:
            content = tokens.pop(); need(content.kind == "string" and token.end == content.start)
            need(len(content.value) % 2 == 0 and re.fullmatch(r"[0-9a-fA-F]*", content.value) is not None)
            return {"type": "blob", "bytes": len(content.value) // 2}
        need(False)
    if token.kind == "hex":
        need(len(token.value) % 2 == 0); return {"type": "blob", "bytes": len(token.value) // 2}
    prefix = ""
    if token.kind in {"+", "-"}:
        prefix = token.value; token = tokens.pop(); need(token.kind == "number")
    need(token.kind == "number" and len(prefix + token.value) <= 128)
    return {"type": "number-literal", "value": prefix + token.value}


def _create(raw, tokens, dialect):
    need(len(tokens) <= MAX_SCHEMA_TOKENS)
    syntax = Tokens(tokens, dialect); syntax.word("CREATE"); syntax.word("TABLE")
    if syntax.is_word("IF"): syntax.word("IF"); syntax.word("NOT"); syntax.word("EXISTS")
    label = syntax.table(); need(syntax.peek() is not None and syntax.peek().kind == "(")
    import sqlglot
    from sqlglot import exp
    need(sqlglot.__version__ == PARSER_VERSION)
    # Only the explicit in-package dialect names above can reach SQLGlot. No
    # plugin dialect entry point, transpiler, optimizer or executor is used.
    logger = logging.getLogger("sqlglot"); disabled = logger.disabled
    try:
        # SQLGlot's unsupported-command warning can contain the source SQL.
        # This reader runs in a dedicated one-shot process; restore its logger
        # flag even on an error, and never emit untrusted source diagnostics.
        logger.disabled = True
        ast = sqlglot.parse_one(raw, read=dialect, error_level=sqlglot.ErrorLevel.RAISE)
    finally:
        logger.disabled = disabled
    need(type(ast) is exp.Create and ast.args.get("kind") == "TABLE" and type(ast.this) is exp.Schema)
    for key, val in ast.args.items():
        if key not in {"this", "kind", "properties", "exists"}: need(not val)
    need(all(type(node).__name__ in _AST_ALLOWED for node in ast.walk()))
    columns = []
    for node in ast.this.expressions:
        if type(node).__name__ in {"PrimaryKey", "UniqueColumnConstraint", "Constraint"}: continue
        need(type(node) is exp.ColumnDef and type(node.this) is exp.Identifier)
        need(not node.args.get("position") and type(node.kind) is exp.DataType)
        need(not node.kind.args.get("nested") and node.kind.this.name not in {"USERDEFINED", "UNKNOWN", "OBJECT", "ARRAY", "MAP", "STRUCT"})
        cname = node.this.name
        if dialect == "postgres" and not node.this.args.get("quoted"): cname = cname.lower()
        need(name(cname))
        declared = node.kind.sql(dialect=dialect).upper()
        need(_TYPE.fullmatch(declared) is not None)
        columns.append({"id": len(columns), "label": cname, "declared_type": declared}); need(len(columns) <= 128)
    need(columns and len(set(c["label"].casefold() for c in columns)) == len(columns))
    return {"id": table_id(dialect, label), "label": label, "columns": columns}


def _target(tokens, tables):
    label = tokens.table()
    # MySQL and SQLite identifiers vary by storage/server settings. Require
    # the spelling from CREATE instead of guessing platform case folding.
    need(label in tables); table = tables[label]
    columns = [c["label"] for c in table["columns"]]
    specified = tokens.columns() if tokens.peek() is not None and tokens.peek().kind == "(" else columns
    need(len(specified) == len(columns) and set(specified) == set(columns))
    return table, [columns.index(c) for c in specified]


def _copy_decode(value):
    if value == "\\N": return {"type": "null", "value": None}
    out = bytearray(); i = 0
    while i < len(value):
        char = value[i]; i += 1
        if char != "\\": out.extend(char.encode("utf8")); continue
        need(i < len(value)); char = value[i]; i += 1
        if char in "bfnrtv\\": out.extend({"b": b"\b", "f": b"\f", "n": b"\n", "r": b"\r", "t": b"\t", "v": b"\v", "\\": b"\\"}[char])
        elif char in "01234567":
            digits = char
            while i < len(value) and len(digits) < 3 and value[i] in "01234567": digits += value[i]; i += 1
            number = int(digits, 8); need(number <= 255); out.append(number)
        elif char == "x":
            digits = ""
            while i < len(value) and len(digits) < 2 and value[i] in "0123456789abcdefABCDEF": digits += value[i]; i += 1
            need(digits); out.append(int(digits, 16))
        else: need(False)  # unsupported escapes are never silently transformed
    return _text(out.decode("utf8", errors="strict"))


def _copy_rows(cursor, width):
    text = cursor.text
    # COPY starts on the next physical line, not inside a SQL token stream.
    end = text.find("\n", cursor.pos); need(end >= 0 and text[cursor.pos:end].strip(" \t\r") == "")
    cursor.pos = end + 1
    while cursor.pos < len(text):
        cursor.check(); end = text.find("\n", cursor.pos)
        need(end >= 0)  # an unterminated COPY terminator is not a complete dump
        raw = text[cursor.pos:end]; cursor.pos = end + 1
        if raw.endswith("\r"): raw = raw[:-1]
        if raw == "\\.": return
        need(len(raw.encode("utf8")) <= MAX_COPY_ROW_BYTES)
        fields = raw.split("\t"); need(len(fields) == width)
        yield [_copy_decode(v) for v in fields]
    need(False)


def sql_dump_preview(data, fmt="sql", kind="tree", options=None):
    try:
        selected = validate_sql_dump_options(kind, options)
        need(type(data) is bytes and 1 <= len(data) <= MAX_INPUT and fmt == "sql")
        text = data.decode("utf-8-sig", errors="strict"); need("\x00" not in text)
        cursor = Cursor(text, selected["dialect"])
        tables = {}; counts = {}; rows = []; ids = []
        statements = ignored = source_rows = schema_bytes = 0

        def add_row(table, permutation, row):
            nonlocal source_rows
            source_rows += 1; need(source_rows <= LIMITS["max_source_rows"])
            row_id = counts[table["id"]]; counts[table["id"]] += 1
            if kind != "table" or selected["table"] != table["id"]: return
            need(all(c < len(table["columns"]) for c in selected["columns"]))
            if selected["row_offset"] <= row_id < selected["row_offset"] + selected["row_limit"]:
                rows.append([row[permutation.index(c)] for c in selected["columns"]]); ids.append(str(row_id))

        while (statement := cursor.statement()) is not None:
            values, raw = statement
            statements += 1; need(statements <= LIMITS["max_statements"])
            if not values: ignored += 1; continue
            tokens = Tokens(values, selected["dialect"])
            if tokens.is_word("CREATE"):
                table = _create(raw, values, selected["dialect"])
                need(table["label"] not in tables and all(t["label"].casefold() != table["label"].casefold() for t in tables.values()))
                tables[table["label"]] = table; counts[table["id"]] = 0; need(len(tables) <= 32)
                schema_bytes += len(raw.encode("utf8")); need(schema_bytes <= LIMITS["max_schema_bytes"])
            elif tokens.is_word("INSERT"):
                tokens.word("INSERT"); tokens.word("INTO"); table, permutation = _target(tokens, tables)
                tokens.word("VALUES")
                while True:
                    tokens.symbol("("); row = [_literal(tokens)]
                    while tokens.peek() is not None and tokens.peek().kind == ",":
                        tokens.pop(); row.append(_literal(tokens)); need(len(row) <= 128)
                    tokens.symbol(")"); need(len(row) == len(permutation)); add_row(table, permutation, row)
                    if tokens.done(): break
                    tokens.symbol(",")
            elif tokens.is_word("COPY"):
                need(selected["dialect"] == "postgres")
                tokens.word("COPY"); table, permutation = _target(tokens, tables)
                tokens.word("FROM"); tokens.word("STDIN"); need(tokens.done())
                for row in _copy_rows(cursor, len(permutation)): add_row(table, permutation, row)
            else:
                # The supported dump metadata families are skipped, not
                # interpreted. Reject compound control forms whose internal
                # semicolons could manufacture apparent top-level data rows.
                first = tokens.pop(); need(first.kind == "word" and first.value.upper() in _IGNORABLE)
                words = [t.value.upper() for t in values if t.kind == "word"]
                if first.value.upper() == "DO":
                    need(selected["dialect"] == "postgres")
                    if tokens.is_word("LANGUAGE"): tokens.word("LANGUAGE"); tokens.identifier()
                    need(tokens.pop().kind in {"dollar", "string"})
                    if tokens.is_word("LANGUAGE"): tokens.word("LANGUAGE"); tokens.identifier()
                    need(tokens.done())
                else: need(not any(w in {"BEGIN", "END", "DELIMITER"} for w in words[1:]))
                if first.value.upper() in {"BEGIN", "COMMIT", "ROLLBACK"}: need(words in [[first.value.upper()], [first.value.upper(), "TRANSACTION"]])
                ignored += 1
        need(tables)
        choices = sorted(tables.values(), key=lambda t: t["label"])
        page = None
        if kind == "table":
            target = next((t for t in choices if t["id"] == selected["table"]), None); need(target is not None)
            need(all(c < len(target["columns"]) for c in selected["columns"]))
            page = {"column_ids": list(selected["columns"]), "columns": [target["columns"][c]["label"] for c in selected["columns"]],
                "rows": rows, "row_ids": ids, "row_offset": selected["row_offset"],
                "has_more": counts[target["id"]] > selected["row_offset"] + len(rows)}
        cursor.check()
        return build_sql_dump_payload(len(data), kind, selected, choices,
            {"statement_count": statements, "ignored_statement_count": ignored, "source_rows": source_rows, "schema_bytes": schema_bytes}, page)
    except Exception:
        # Never leak SQLGlot's offending source span or a host path in errors.
        raise SqlDumpError(ERROR) from None
