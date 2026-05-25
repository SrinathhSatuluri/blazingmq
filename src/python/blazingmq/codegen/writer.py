# Copyright 2024 Bloomberg Finance L.P.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Lightweight code writer with indentation tracking."""

from __future__ import annotations


class Writer:
    """Accumulates lines of generated code with indentation support."""

    def __init__(self) -> None:
        self._lines: list[str] = []
        self._indent: int = 0

    def indent(self) -> None:
        self._indent += 1

    def dedent(self) -> None:
        self._indent -= 1

    def line(self, text: str = "") -> None:
        if text:
            self._lines.append("    " * self._indent + text)
        else:
            self._lines.append("")

    def raw(self, text: str) -> None:
        """Append pre-formatted text (no indentation applied)."""
        self._lines.append(text)

    def banner(self, class_name: str) -> None:
        """Emit a BDE-style class banner comment (equals signs)."""
        bar = "=" * (len(class_name) + 6)
        self.line(f"// {bar}")
        self.line(f"// class {class_name}")
        self.line(f"// {bar}")

    def separator(self, class_name: str) -> None:
        """Emit a BDE-style class separator comment (dashes)."""
        bar = "-" * (len(class_name) + 6)
        self.line(f"// {bar}")
        self.line(f"// class {class_name}")
        self.line(f"// {bar}")

    def comment(self, text: str, indent: int = 4, max_width: int = 75) -> None:
        """Emit a wrapped BDE-style comment block.

        Wraps `text` into lines of the form ``{indent}// {words}`` such that
        no line exceeds `max_width` characters (BDE convention: 75 for doc
        comments).  Double spaces between sentences are preserved: when the
        empty word from a double space wraps to a new line, the following
        word picks up the leading space naturally via ``" ".join``.
        """
        prefix = " " * indent + "// "
        max_text = max_width - len(prefix)

        words = text.split(" ")

        lines: list[str] = []
        current: list[str] = []
        current_len = 0

        for word in words:
            wlen = len(word)
            if current and current_len + 1 + wlen > max_text:
                lines.append(prefix + " ".join(current))
                current = [word]
                current_len = len(word)
            else:
                if current:
                    current_len += 1 + wlen
                else:
                    current_len = wlen
                current.append(word)

        if current:
            lines.append(prefix + " ".join(current))

        for line in lines:
            self._lines.append(line.rstrip())

    def wrapped(self, text: str, max_width: int = 79) -> None:
        """Emit a line, wrapping at parameter boundaries if it exceeds *max_width*.

        When wrapping is needed:
        - The first parameter stays on the opening line.
        - Continuation lines are indented to the column after the opening ``(``.
        - If parameters have type+name structure, types are padded so
          parameter names align at the same column (BDE convention).
        - If still too long, split return type from function name.
        """
        effective = "    " * self._indent + text if text else ""
        if len(effective) <= max_width:
            self.line(text)
            return

        # Compute both strategies up front so we can compare line counts.
        split = _split_return_type(effective, max_width)
        param_lines = _wrap_params(effective, max_width)
        is_paren_aligned = len(param_lines) > 1 and not param_lines[
            0
        ].rstrip().endswith("(")
        paren_ok = is_paren_aligned and all(len(ln) <= max_width for ln in param_lines)

        # Strategy 1: Return-type split when ALL params fit on the second
        # line AND it produces fewer lines than paren-aligned wrapping.
        # (e.g. 3-param print() → 2 lines via split vs 3 via paren-aligned.)
        if split and len(split[1]) <= max_width:
            paren_count = len(param_lines) if paren_ok else float("inf")
            if 2 < paren_count:
                for ln in split:
                    self._lines.append(ln)
                return

        # Strategy 2: Parameter-list wrapping (clang-format preference:
        # keep return type + function name together, wrap params at '(').
        # Only use paren-aligned when the first param stays on the opening
        # line (not the fallback "all on new lines" style).
        if paren_ok:
            for ln in param_lines:
                self._lines.append(ln)
            return

        # Strategy 3: Return-type split (BDE convention — preferred over
        # fallback paren wrapping for function definitions).
        if split:
            result_lines = [split[0]]
            if len(split[1]) > max_width:
                result_lines.extend(_wrap_params(split[1], max_width))
            else:
                result_lines.append(split[1])
            # Only use return-type split if it doesn't produce more lines
            # than the fallback paren wrapping (which keeps return type
            # together with function name).
            fallback_ok = all(len(ln) <= max_width for ln in param_lines)
            if not fallback_ok or len(result_lines) <= len(param_lines):
                for ln in result_lines:
                    self._lines.append(ln)
                return

        # Strategy 4: Fallback paren wrapping (all params on new lines).
        if all(len(ln) <= max_width for ln in param_lines):
            for ln in param_lines:
                self._lines.append(ln)
            return

        # Final fallback: use whatever _wrap_params produced, even if overlong.
        for ln in param_lines:
            self._lines.append(ln)

    def result(self) -> str:
        return "\n".join(self._lines) + "\n"


# ── Return-type splitting ────────────────────────────────────────────


def _split_return_type(line: str, max_width: int = 79) -> list[str] | None:
    """Split a long line at the return-type / function-name boundary.

    Handles two patterns:
    1. Qualified definitions: ``inline const Foo& Class::method() const``
       → ``inline const Foo&`` / ``Class::method() const``
    2. In-class declarations: ``    Type& operator=(const Type& rhs);``
       → ``    Type&`` / ``    operator=(const Type& rhs);``

    Returns a list of two lines, or None if splitting isn't applicable.
    """
    stripped = line.lstrip()
    indent = line[: len(line) - len(stripped)]

    # Must contain a function call — look for '('
    if "(" not in stripped:
        return None

    # Strategy 1: qualified function name — :: appears BEFORE ( in the token
    # (e.g. "ClassName::method(" but NOT "Name(bsl::string")
    tokens = stripped.split(" ")
    split_idx = None
    for i, tok in enumerate(tokens):
        paren_pos = tok.find("(")
        colon_pos = tok.find("::")
        if colon_pos != -1 and paren_pos != -1 and colon_pos < paren_pos and i > 0:
            split_idx = i
            break

    # Strategy 2: in-class declaration — find the function name token
    # (contains "(") that isn't a type like bsl::vector<T>
    if split_idx is None:
        for i, tok in enumerate(tokens):
            if "(" in tok and "::" not in tok and i > 0:
                # Verify the preceding token looks like a return type
                # (ends with &, *, >, or is a keyword)
                prev = tokens[i - 1]
                if prev.endswith(("&", "*", ">")):
                    split_idx = i
                    break

    if split_idx is None:
        return None

    return_type = " ".join(tokens[:split_idx])
    func_part = " ".join(tokens[split_idx:])

    line1 = indent + return_type
    line2 = indent + func_part

    # Only produce a split if line1 fits (line2 may need further wrapping).
    if len(line1) <= max_width:
        return [line1, line2]

    return None


# ── Parameter-list wrapping ──────────────────────────────────────────


def _split_at_commas(text: str) -> list[str]:
    """Split *text* at top-level commas (respecting ``<>`` and ``()`` nesting)."""
    parts: list[str] = []
    current: list[str] = []
    depth_a = 0  # angle brackets
    depth_p = 0  # parentheses
    for ch in text:
        if ch == "<":
            depth_a += 1
        elif ch == ">":
            depth_a -= 1
        elif ch == "(":
            depth_p += 1
        elif ch == ")":
            depth_p -= 1
        elif ch == "," and depth_a == 0 and depth_p == 0:
            parts.append("".join(current).strip())
            current = []
            continue
        current.append(ch)
    if current:
        parts.append("".join(current).strip())
    return parts


def _parse_param(param: str) -> tuple[str, str]:
    """Split a C++ parameter into ``(type, name_with_default)``.

    Returns ``("", param)`` when no type can be extracted (bare identifiers,
    expressions like ``&d_member``).
    """
    # Bare identifier or expression (no spaces, or starts with & / *)
    stripped = param.strip()
    if " " not in stripped or stripped.startswith("&") or stripped.startswith("*"):
        return ("", stripped)

    # Strip default value first (everything from " = " onwards)
    eq_pos = stripped.find(" = ")
    if eq_pos != -1:
        before_default = stripped[:eq_pos]
        default_part = stripped[eq_pos:]
    else:
        before_default = stripped
        default_part = ""

    # The name is the last token in before_default
    last_space = before_default.rindex(" ")
    type_part = before_default[:last_space].rstrip()
    name_part = before_default[last_space:].lstrip() + default_part
    return (type_part, name_part)


def _wrap_params(line: str, max_width: int = 79) -> list[str]:
    """Wrap *line* at parameter boundaries if it exceeds *max_width*.

    Returns a list of output lines. If no wrapping is needed, returns
    ``[line]``.
    """
    if len(line) <= max_width:
        return [line]

    # Find the first top-level '(' (skip angle brackets but not << / >>)
    paren_pos: int | None = None
    angle = 0
    i = 0
    while i < len(line):
        ch = line[i]
        # Skip << and >> (shift / insertion operators, not angle brackets)
        if ch == "<" and i + 1 < len(line) and line[i + 1] == "<":
            i += 2
            continue
        if ch == ">" and i + 1 < len(line) and line[i + 1] == ">":
            i += 2
            continue
        if ch == "<":
            angle += 1
        elif ch == ">":
            angle -= 1
        elif ch == "(" and angle == 0:
            paren_pos = i
            break
        i += 1

    if paren_pos is None:
        return [line]

    # Find matching ')'
    depth = 0
    close_pos: int | None = None
    for i in range(paren_pos, len(line)):
        if line[i] == "(":
            depth += 1
        elif line[i] == ")":
            depth -= 1
            if depth == 0:
                close_pos = i
                break

    if close_pos is None:
        return [line]

    prefix = line[: paren_pos + 1]
    params_str = line[paren_pos + 1 : close_pos]
    suffix = line[close_pos:]  # ')' and anything after

    params = _split_at_commas(params_str)
    if len(params) == 0:
        return [line]

    # Single-param case: wrap as FUNC(\n    arg) if the line is too long
    if len(params) == 1:
        stripped = line.lstrip()
        base_indent = len(line) - len(stripped)
        fallback_indent = " " * (base_indent + 4)
        wrapped_line = fallback_indent + params[0].strip() + suffix
        if len(wrapped_line) <= max_width:
            return [prefix, wrapped_line]
        return [line]

    cont_indent = " " * (paren_pos + 1)

    # Parse params for type+name alignment
    parsed = [_parse_param(p) for p in params]
    typed = [t for t, _ in parsed if t]

    # Per-line type padding: pad each param's type when all params have a
    # type component and the padded result fits within max_width.  Lines that
    # would exceed the limit are left unpadded (clang-format behaviour).
    max_type_w = 0
    can_pad = len(typed) == len(parsed) and len(typed) > 1
    if can_pad:
        max_type_w = max(len(t) for t in typed)

    lines: list[str] = []
    for i, (type_part, name_part) in enumerate(parsed):
        is_last = i == len(parsed) - 1
        line_prefix = prefix if i == 0 else cont_indent
        extra = suffix if is_last else ","

        if can_pad and type_part:
            padded_body = type_part.ljust(max_type_w) + " " + name_part
            if len(line_prefix + padded_body + extra) <= max_width:
                body = padded_body
            else:
                body = (type_part + " " + name_part).strip()
        else:
            body = (type_part + " " + name_part).strip() if type_part else name_part

        text = line_prefix + body + extra
        lines.append(text)

    # Check if paren-aligned wrapping fits — if any continuation line
    # exceeds max_width, fall back to "all params on new lines" style:
    #   prefix(
    #       param1,
    #       param2);
    if any(len(ln) > max_width for ln in lines):
        # Before falling back, try bracket-splitting overlong paren-aligned
        # lines.  Bloomberg's codegen prefers the more compact paren-aligned
        # layout when bracket-splitting (at ``[``) makes everything fit.
        pa_bracket: list[str] = []
        for ln in lines:
            if len(ln) > max_width and "[" in ln:
                bp = ln.index("[")
                pre_b = ln[:bp]
                post_b = ln[bp:]
                il = len(ln) - len(ln.lstrip())
                si = " " * (il + 4)
                if len(pre_b) <= max_width and len(si + post_b) <= max_width:
                    pa_bracket.append(pre_b)
                    pa_bracket.append(si + post_b)
                    continue
            pa_bracket.append(ln)
        pa_bracket_ok = all(len(ln) <= max_width for ln in pa_bracket)

        # Determine base indentation from the line
        stripped = line.lstrip()
        base_indent = len(line) - len(stripped)
        fallback_indent = " " * (base_indent + 4)

        # Per-line type padding in fallback layout
        fb_typed = [t for t, _ in parsed if t]
        fb_can_pad = len(fb_typed) == len(parsed) and len(fb_typed) > 1
        fb_max_type_w = max(len(t) for t in fb_typed) if fb_can_pad else 0

        fb_lines: list[str] = [prefix]
        for i, (type_part, name_part) in enumerate(parsed):
            is_last = i == len(parsed) - 1
            extra = suffix if is_last else ","

            if fb_can_pad and type_part:
                padded_body = type_part.ljust(fb_max_type_w) + " " + name_part
                if len(fallback_indent + padded_body + extra) <= max_width:
                    body = padded_body
                else:
                    body = (type_part + " " + name_part).strip()
            else:
                body = (type_part + " " + name_part).strip() if type_part else name_part
            text = fallback_indent + body + extra
            fb_lines.append(text)

        # Apply bracket-split to fallback lines too
        fb_final: list[str] = []
        for ln in fb_lines:
            if len(ln) > max_width and "[" in ln:
                bp2 = ln.index("[")
                pre_b2 = ln[:bp2]
                post_b2 = ln[bp2:]
                il2 = len(ln) - len(ln.lstrip())
                si2 = " " * (il2 + 4)
                if len(si2 + post_b2) <= max_width:
                    fb_final.append(pre_b2)
                    fb_final.append(si2 + post_b2)
                    continue
            fb_final.append(ln)

        # Prefer paren-aligned + bracket-split when strictly more compact
        if pa_bracket_ok and len(pa_bracket) < len(fb_final):
            return pa_bracket

        lines = fb_lines

    # Final check: if any fallback line STILL exceeds max_width and contains
    # an array subscript (e.g. INFO_ARRAY[INDEX]), split at '['.
    final_lines: list[str] = []
    for ln in lines:
        if len(ln) > max_width and "[" in ln:
            bracket_pos = ln.index("[")
            # Only split if the bracket is in the value portion (not near start)
            pre_bracket = ln[:bracket_pos]
            post_bracket = ln[bracket_pos:]
            indent_of_line = len(ln) - len(ln.lstrip())
            sub_indent = " " * (indent_of_line + 4)
            if len(sub_indent + post_bracket) <= max_width:
                final_lines.append(pre_bracket)
                final_lines.append(sub_indent + post_bracket)
                continue
        final_lines.append(ln)

    return final_lines
