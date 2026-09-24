"""An explicit static SVG CSS subset, normalized before offline rendering.

No browser CSS guessing: only compound type/class/id/universal selectors,
comma-separated selector lists and known SVG presentation properties are
accepted. Unsupported selectors, at-rules and resource functions fail closed.
"""
import math
import re


class SvgStyleError(ValueError):
    pass


PROPERTIES = frozenset("""color display visibility opacity fill fill-opacity fill-rule stroke
stroke-opacity stroke-width stroke-linecap stroke-linejoin stroke-miterlimit stroke-dasharray
stroke-dashoffset clip-rule clip-path mask marker marker-start marker-mid marker-end
font-family font-size font-style font-weight font-variant font-stretch text-anchor
dominant-baseline alignment-baseline letter-spacing word-spacing text-decoration
shape-rendering text-rendering image-rendering vector-effect paint-order""".split())
MAX_RULES = 256
MAX_STYLE_BYTES = 256_000
MAX_MATCHES = 1_000_000


def xml_name(value: str) -> bool:
    def start(char):
        n = ord(char)
        return (char in ":_" or 65 <= n <= 90 or 97 <= n <= 122
            or 0xC0 <= n <= 0xD6 or 0xD8 <= n <= 0xF6 or 0xF8 <= n <= 0x2FF
            or 0x370 <= n <= 0x37D or 0x37F <= n <= 0x1FFF or 0x200C <= n <= 0x200D
            or 0x2070 <= n <= 0x218F or 0x2C00 <= n <= 0x2FEF or 0x3001 <= n <= 0xD7FF
            or 0xF900 <= n <= 0xFDCF or 0xFDF0 <= n <= 0xFFFD or 0x10000 <= n <= 0xEFFFF)
    return bool(value) and start(value[0]) and all(start(char) or char in "-."
        or "0" <= char <= "9" or ord(char) == 0xB7 or 0x300 <= ord(char) <= 0x36F
        or 0x203F <= ord(char) <= 0x2040 for char in value[1:])


def local_reference(value: str) -> bool:
    return value.startswith("#") and xml_name(value[1:])


def resource_value(value: str, references: list[str]) -> None:
    lowered = value.lower()
    if any(marker in lowered for marker in ("\\", "@", "/*", "*/", "<", ">", "{", "}")):
        raise SvgStyleError()
    # CSS image-set() permits quoted external addresses WITHOUT url(). All
    # functions except the explicitly supported colors/local references fail.
    remaining = value
    for match in re.finditer(r"([\w-]+)\s*\(([^()]*)\)", value):
        name, argument = match[1].lower(), match[2].strip()
        if name == "url":
            if argument[:1] in {"'", '"'}:
                if len(argument) < 2 or argument[-1] != argument[0]:
                    raise SvgStyleError()
                argument = argument[1:-1]
            if not local_reference(argument):
                raise SvgStyleError()
            references.append(argument[1:])
        elif name not in {"rgb", "rgba", "hsl", "hsla"} or not re.fullmatch(r"[0-9.eE+,% /-]+", argument):
            raise SvgStyleError()
        remaining = remaining.replace(match[0], "", 1)
    if "(" in remaining or ")" in remaining:
        raise SvgStyleError()


def declarations(value: str, references: list[str]):
    if len(value) > MAX_STYLE_BYTES:
        raise SvgStyleError()
    result = []
    for item in value.split(";"):
        if not item.strip():
            continue
        key, sep, raw = item.partition(":")
        key, raw = key.strip().lower(), raw.strip()
        if not sep or key not in PROPERTIES or not raw:
            raise SvgStyleError()
        important = bool(re.search(r"!\s*important\s*$", raw, re.I))
        raw = re.sub(r"!\s*important\s*$", "", raw, flags=re.I).strip()
        if "!" in raw:
            raise SvgStyleError()
        resource_value(raw, references)
        result.append((key, raw, important))
    return result


def selector(value: str):
    # Deliberately not a partial implementation of combinators/pseudo-classes.
    # Unsupported syntax is rejected, never ignored by the validation renderer.
    value = value.strip()
    if not value or any(char.isspace() for char in value) or any(char in value for char in ":[]()>+~\\"):
        raise SvgStyleError()
    tokens = re.split(r"([.#])", value)
    tag, constraints = tokens[0], []
    if tag and tag != "*" and not xml_name(tag):
        raise SvgStyleError()
    if len(tokens) % 2 == 0:
        raise SvgStyleError()
    for index in range(1, len(tokens), 2):
        if not xml_name(tokens[index + 1]):
            raise SvgStyleError()
        constraints.append((tokens[index], tokens[index + 1]))
    if not tag and not constraints:
        raise SvgStyleError()
    return tag, constraints, (0, sum(kind == "#" for kind, _ in constraints),
        sum(kind == "." for kind, _ in constraints), int(tag not in {"", "*"}))


def apply_static_styles(root, references: list[str], check) -> None:
    rules, sheets, byte_count = [], [], 0
    for node in root.iter():
        check()
        if node.tag.rsplit("}", 1)[-1] != "style":
            continue
        if node.get("type", "text/css") != "text/css" or node.get("media"):
            raise SvgStyleError()
        sheets.append(node)
        text = "".join(node.itertext())
        byte_count += len(text.encode("utf-8"))
        if byte_count > MAX_STYLE_BYTES:
            raise SvgStyleError()
        end = 0
        for match in re.finditer(r"([^{}]+)\{([^{}]*)\}", text):
            if text[end:match.start()].strip():
                raise SvgStyleError()
            for pattern in match[1].split(","):
                rules.append((selector(pattern), declarations(match[2], references)))
                if len(rules) > MAX_RULES:
                    raise SvgStyleError()
            end = match.end()
        if text[end:].strip():
            raise SvgStyleError()
    checks = 0
    for node in root.iter():
        check()
        values = {key: ((False, (-1, 0, 0, 0), -1), value)
            for key, value in node.attrib.items() if key in PROPERTIES}
        for order, ((tag, constraints, specificity), entries) in enumerate(rules):
            checks += 1
            if checks > MAX_MATCHES:
                raise SvgStyleError()
            if tag not in {"", "*", node.tag.rsplit("}", 1)[-1]}:
                continue
            if any((node.get("id") != name if kind == "#" else name not in node.get("class", "").split())
                   for kind, name in constraints):
                continue
            for key, value, important in entries:
                rank = (important, specificity, order)
                if key not in values or rank >= values[key][0]:
                    values[key] = (rank, value)
        for key, value, important in declarations(node.get("style", ""), references):
            rank = (important, (1, 0, 0, 0), len(rules))
            if key not in values or rank >= values[key][0]:
                values[key] = (rank, value)
        node.attrib.pop("style", None)
        node.attrib.update({key: value for key, (_, value) in values.items()})
    for parent in root.iter():
        for child in list(parent):
            if child in sheets:
                parent.remove(child)


def enforce_visibility(root, drawable: set[str], check) -> int:
    """Resolve supported visibility inheritance; prune non-painting geometry."""
    count = 0
    pending = [(root, None, "visible", False, 1.0)]
    while pending:
        check()
        node, parent, inherited_visibility, hidden, parent_opacity = pending.pop()
        visibility = node.get("visibility", "inherit").strip().lower()
        if visibility in {"inherit", "unset"}:
            visibility = inherited_visibility
        elif visibility == "initial":
            visibility = "visible"
        if visibility not in {"visible", "hidden", "collapse"}:
            raise SvgStyleError()
        display = node.get("display", "inline").strip().lower()
        if display not in {"none", "inline", "block", "initial", "inherit", "unset"}:
            raise SvgStyleError()
        opacity_text = node.get("opacity", "1").strip().lower()
        if opacity_text in {"initial", "unset"}:
            opacity = 1.0
        elif opacity_text == "inherit":
            opacity = parent_opacity
        else:
            try:
                opacity = float(opacity_text.rstrip("%")) / (100 if opacity_text.endswith("%") else 1)
            except ValueError:
                raise SvgStyleError() from None
        if not math.isfinite(opacity):
            raise SvgStyleError()
        hidden = hidden or display == "none" or opacity <= 0
        local = node.tag.rsplit("}", 1)[-1].lower()
        if hidden:
            if parent is not None:
                parent.remove(node)
            continue
        node.set("visibility", visibility)
        if local in drawable and visibility == "visible":
            count += 1
        elif local in drawable:
            # Remove hidden paint without discarding visible descendants that
            # explicitly override inherited visibility (for example tspan).
            node.set("fill", "none")
            node.set("stroke", "none")
            if local in {"image", "use"}:
                if parent is not None:
                    parent.remove(node)
                continue
        pending.extend((child, node, visibility, False, opacity) for child in reversed(list(node)))
    return count
