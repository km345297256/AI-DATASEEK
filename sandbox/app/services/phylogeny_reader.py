"""Restricted single-tree Newick grammar, never XML/commands/native plugins.

Primary grammar: https://phylipweb.github.io/phylip/newick_doc.html
Deliberately rejects comments/NHX, negative lengths, multi-tree containers.
"""
from .phylogeny_payload import (FORMATS, MAX_DEPTH, MAX_INPUT, MAX_NODES, PhylogenyError,
    WARNINGS, label, length, require, tree_statistics, validate_phylogeny_options, validate_phylogeny_payload)


class Newick:
    def __init__(self, text):
        self.text, self.position, self.nodes = text, 0, []

    def space(self):
        while self.position < len(self.text) and self.text[self.position] in " \t\r\n":
            self.position += 1

    def peek(self):
        self.space()
        return self.text[self.position:self.position + 1]

    def name(self):
        self.space()
        start = self.position
        if self.text[start:start + 1] == "'":
            self.position += 1
            parts = []
            while self.position < len(self.text):
                value = self.text[self.position]
                self.position += 1
                if value == "'":
                    if self.text[self.position:self.position + 1] == "'":
                        self.position += 1
                        value = "'"
                    else:
                        return label("".join(parts) or None)
                parts.append(value)
                require(len(parts) <= 256, "系统树标签超出 256 字符限制。")
            require(False, "系统树引号标签未结束。")
        while self.position < len(self.text):
            value = self.text[self.position]
            if value in " \t\r\n(),:;[]'":
                break
            self.position += 1
            require(self.position - start <= 256, "系统树标签超出 256 字符限制。")
        return label(self.text[start:self.position].replace("_", " ") or None)

    def branch(self):
        if self.peek() != ":":
            return None
        self.position += 1
        self.space()
        start = self.position
        while self.position < len(self.text) and self.text[self.position] not in " \t\r\n(),;[]":
            self.position += 1
            require(self.position - start <= 32, "枝长原文超出 32 字符限制。")
        value = self.text[start:self.position]
        length(value)
        return value

    def subtree(self, parent=None, depth=0):
        require(depth <= MAX_DEPTH and len(self.nodes) < MAX_NODES, "系统树超过 1000 节点或 64 层限制。")
        node = {"id": f"n{len(self.nodes)}", "parent": parent, "label": None, "length": None}
        self.nodes.append(node)
        if self.peek() == "(":
            self.position += 1
            self.subtree(node["id"], depth + 1)
            while self.peek() == ",":
                self.position += 1
                self.subtree(node["id"], depth + 1)
            require(self.peek() == ")", "系统树括号或分支分隔符无效。")
            self.position += 1
        node["label"] = self.name()
        node["length"] = self.branch()
        return node

    def parse(self):
        require(self.peek() == "(", "仅支持以括号开始的单棵 Newick；不支持 NEXUS、phyloXML 或注释。")
        self.subtree()
        require(self.peek() == ";", "单棵 Newick 必须以分号结束；不支持注释或 NHX。")
        self.position += 1
        require(self.peek() == "", "仅支持一棵树；多树与尾随内容必须先显式拆分。")
        return self.nodes


def phylogeny_preview(data, fmt, options=None):
    validate_phylogeny_options("tree", {} if options is None else options)
    require(type(data) is bytes and 1 <= len(data) <= MAX_INPUT, "系统树输入必须为 4 MiB 内的 UTF-8 文本。")
    require(type(fmt) is str and fmt in FORMATS, "仅支持 nwk、newick、tree 和 tre 的单棵 Newick。")
    try:
        text = data.decode("utf-8-sig", errors="strict")
        nodes = Newick(text).parse()
        value = {"contract_version": 2, "type": "phylogeny", "reader": "phylogeny", "kind": "tree",
                 "media_type": "application/json", "phylogeny": {"root": "n0", "rootedness": "unspecified", "nodes": nodes},
                 "metadata": {"format": fmt, "dialect": "newick-single-v1", "input_mode": "whole",
                              "source_bytes": len(data), **tree_statistics(nodes)},
                 "warnings": WARNINGS[:], "sampled": False}
        return validate_phylogeny_payload(value, size=len(data), fmt=fmt)
    except PhylogenyError:
        raise
    except Exception:
        raise PhylogenyError("系统树解析失败；请检查单树格式、字符和资源限制。") from None

