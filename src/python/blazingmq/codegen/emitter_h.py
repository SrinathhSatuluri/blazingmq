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

"""Emit the .h file for a BDE-style generated component."""

from __future__ import annotations

import re

from .model import (
    ComplexType,
    EnumType,
    Field,
    FieldKind,
    Schema,
    TypeKind,
    XSD_TYPE_MAP,
)
from .naming import (
    make_banner,
    to_class_name,
    to_enum_value,
    to_member_name,
    to_upper_snake,
)
from .hybrid import expand_hybrids
from .ordering import sort_fields_by_alignment, topological_sort_types
from .resolver import ResolvedField, resolve_field, type_needs_allocator
from .writer import Writer

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_includes(schema: Schema) -> str:
    """Build the include block, only emitting headers for types actually used."""
    # Collect all XSD type names used in the schema
    used_xsd_types: set[str] = set()
    has_nullable_allocated = False
    has_nullable = False
    for t in schema.types:
        if isinstance(t, ComplexType):
            for f in t.fields:
                used_xsd_types.add(f.type_name)
                if f.kind == FieldKind.OPTIONAL:
                    # Check if self-referential (uses NullableAllocatedValue)
                    ref_name = f.type_name.removeprefix("tns:")
                    if (
                        any(
                            inner_f.type_name.removeprefix("tns:") == t.name
                            for inner_t in schema.types
                            if isinstance(inner_t, ComplexType)
                            and inner_t.name == ref_name
                            for inner_f in inner_t.fields
                        )
                        or ref_name == t.name
                    ):
                        has_nullable_allocated = True
                    else:
                        has_nullable = True

    # Map XSD types to needed includes
    has_any_datetime = bool({"xs:date", "xs:time", "xs:dateTime"} & used_xsd_types)
    needs_date = has_any_datetime
    needs_time = has_any_datetime
    needs_datetime = has_any_datetime

    lines = [
        "#include <bslalg_typetraits.h>",
        "",
        "#include <bdlat_attributeinfo.h>",
        "",
        "#include <bdlat_enumeratorinfo.h>",
        "",
        "#include <bdlat_selectioninfo.h>",
        "",
        "#include <bdlat_typetraits.h>",
        "",
        "#include <bslh_hash.h>",
        "#include <bsls_objectbuffer.h>",
        "",
        "#include <bslma_default.h>",
        "",
        "#include <bsls_assert.h>",
    ]

    if has_nullable_allocated:
        lines += ["", "#include <bdlb_nullableallocatedvalue.h>"]

    if has_nullable or has_nullable_allocated:
        lines += ["", "#include <bdlb_nullablevalue.h>"]

    if needs_datetime:
        lines += ["", "#include <bdlt_datetimetz.h>"]
    if needs_date:
        lines += ["", "#include <bdlt_datetz.h>"]
    if needs_time:
        lines += ["", "#include <bdlt_timetz.h>"]

    lines += [
        "",
        "#include <bsl_string.h>",
        "",
        "#include <bsl_vector.h>",
        "",
        "#include <bsls_types.h>",
        "",
        "#include <bsl_iosfwd.h>",
        "#include <bsl_limits.h>",
        "#include <bsl_type_traits.h>",
        "",
        "#include <bsl_ostream.h>",
        "#include <bsl_string.h>",
    ]

    return "\n".join(lines)


_BY_VALUE_TYPES = {
    "bool",
    "int",
    "unsigned int",
    "double",
    "bsls::Types::Int64",
    "bsls::Types::Uint64",
}


def _normalize_sentence_spacing(text: str) -> str:
    """Normalize to BDE double-space convention after sentence-ending periods.

    Replace single spaces after periods with double spaces, except when the
    period is part of a sequence of dots (like ``rebalance..:``) or already
    followed by two spaces.
    """
    return re.sub(r"(?<!\.)\.(?!\.) (?! )", ".  ", text)


def _emit_class_doc(w: Writer, doc: str) -> None:
    """Emit multi-line class documentation comment, wrapped at 79 chars.

    Class documentation in BDE wraps at 79 characters total (72 text width),
    which is wider than the 75-char limit for method doc comments.
    Paragraphs (separated by blank lines) are joined and re-wrapped.
    Double spaces after periods are enforced (BDE convention).
    """
    # Split into paragraphs (separated by blank lines)
    paragraphs = []
    current_para: list[str] = []
    for line in doc.split("\n"):
        stripped = line.strip()
        if not stripped:
            if current_para:
                paragraphs.append(" ".join(current_para))
                current_para = []
        else:
            current_para.append(stripped)
    if current_para:
        paragraphs.append(" ".join(current_para))

    # Emit each paragraph wrapped at 79 chars with BDE double-space convention
    for para in paragraphs:
        w.comment(_normalize_sentence_spacing(para), indent=4, max_width=79)


def _accessor_return(rf: ResolvedField) -> tuple[str, str]:
    """Return (mutable_type, const_type) for accessor declarations."""
    base = rf.decl_type
    if rf.field.kind == FieldKind.REGULAR:
        # Primitive types and enums return by value (const) and reference (mutable)
        if rf.field.type_name in XSD_TYPE_MAP:
            cpp, _, _ = XSD_TYPE_MAP[rf.field.type_name]
            if cpp in _BY_VALUE_TYPES:
                return (f"{cpp}&", cpp)
        # Enum types (cpp_type ends with ::Value)
        elif rf.cpp_type.endswith("::Value"):
            return (f"{rf.cpp_type}&", rf.cpp_type)
    return (f"{base}&", f"const {base}&")


def _needs_pointer_storage(f: Field, schema: Schema) -> bool:
    """Check if a regular field needs to be stored as a raw pointer.

    A regular (non-optional, non-array) field is stored as ``T*`` when its
    resolved type is a ComplexType that internally uses
    NullableAllocatedValue (i.e., has at least one self-referential optional
    field).  The containing sequence also gains a ``d_allocator_p`` member
    so that it can manage the heap-allocated object.
    """
    if f.kind != FieldKind.REGULAR:
        return False
    if f.type_name in XSD_TYPE_MAP:
        return False
    ref_name = f.type_name.removeprefix("tns:")
    resolved_type = schema.type_by_name(ref_name)
    if not isinstance(resolved_type, ComplexType):
        return False
    for sub_f in resolved_type.fields + resolved_type.choices:
        rf = resolve_field(sub_f, resolved_type, schema)
        if rf.is_nullable_allocated:
            return True
    return False


def _field_is_primitive(rf: ResolvedField) -> bool:
    """True if the field is a primitive scalar (not string/vector/class)."""
    if rf.field.kind != FieldKind.REGULAR:
        return False
    if rf.field.type_name in XSD_TYPE_MAP:
        cpp, _, _ = XSD_TYPE_MAP[rf.field.type_name]
        return cpp in _BY_VALUE_TYPES
    if rf.cpp_type.endswith("::Value"):
        return True  # Enums are scalar
    return False


# ---------------------------------------------------------------------------
# Alignment helpers
# ---------------------------------------------------------------------------


def _norm_doc(text: str) -> str:
    """Normalise field-documentation text to match Bloomberg conventions.

    Bloomberg's codegen doubles the space after a sentence-ending period
    (``". "`` → ``".  "``).  The XSD may only have a single space; this
    helper normalises that.  Periods that are part of dot-sequences (like
    ``rebalance..:``) are left alone.
    """
    return _normalize_sentence_spacing(text)


def _emit_aligned_union_members(
    w: Writer,
    resolved: list,
    ob_types: list[str],
) -> None:
    """Emit column-aligned union members with doc-comment group breaks
    and overflow wrapping (when a padded line would exceed 79 chars).

    Doc-comment group breaks: a field with a doc comment ends the current
    alignment group; subsequent members form a new group with independent
    alignment.

    Overflow: within a group, if the longest ObjectBuffer type would cause
    any line to exceed 79 characters, exclude overflowing types from the
    alignment width.  Overflowing types are split across two lines (type
    on line 1, member name indented to the alignment column of the
    post-overflow members on line 2).
    """
    # Break into alignment groups (doc comments create breaks)
    groups: list[list[tuple]] = []
    cur: list[tuple] = []
    for rf, ob in zip(resolved, ob_types):
        cur.append((rf, ob))
        if rf.field.doc:
            groups.append(cur)
            cur = []
    if cur:
        groups.append(cur)

    for group in groups:
        g_types = [ob for _, ob in group]
        g_max = max(len(t) for t in g_types)

        # Detect overflow: when padding to g_max would push a line past 79
        overflow: set[str] = set()
        for rf, ob in group:
            member = f"{to_member_name(rf.field.name)};"
            if 8 + g_max + 1 + len(member) > 79:
                overflow.add(ob)

        if not overflow:
            # Simple case: all members fit with uniform alignment
            for rf, ob in group:
                member = f"{to_member_name(rf.field.name)};"
                w.line(f"        {ob.ljust(g_max)} {member}")
                if rf.field.doc:
                    w.comment(_norm_doc(rf.field.doc), indent=8, max_width=79)
        else:
            # Recompute alignment excluding overflow types
            fitting = [t for t in g_types if t not in overflow]
            g_max = max(len(t) for t in fitting) if fitting else 0

            # Compute post-overflow alignment column: the max type width
            # among members that follow the last overflow member.
            last_overflow_idx = -1
            for i, (rf, ob) in enumerate(group):
                if ob in overflow:
                    last_overflow_idx = i
            post_types = [
                ob for j, (_, ob) in enumerate(group) if j > last_overflow_idx
            ]
            post_max = max(len(t) for t in post_types) if post_types else g_max
            overflow_cont_col = 8 + post_max + 1

            found_overflow = False
            for rf, ob in group:
                member = f"{to_member_name(rf.field.name)};"
                if ob in overflow:
                    w.line(f"        {ob}")
                    w.line(f"{' ' * overflow_cont_col}{member}")
                    found_overflow = True
                elif found_overflow:
                    # After overflow: unpadded (new alignment group)
                    w.line(f"        {ob} {member}")
                else:
                    w.line(f"        {ob.ljust(g_max)} {member}")
                if rf.field.doc:
                    w.comment(_norm_doc(rf.field.doc), indent=8, max_width=79)


def _emit_aligned_seq_members(
    w: Writer,
    members: list[tuple[str, str, str | None]],
) -> None:
    """Emit column-aligned sequence members with doc-comment group breaks.

    Same group-break logic as union members: a field with a doc comment
    ends the current alignment group.
    """
    groups: list[list[tuple[str, str, str | None]]] = []
    cur: list[tuple[str, str, str | None]] = []
    for item in members:
        cur.append(item)
        if item[2]:  # non-empty doc creates alignment group break
            groups.append(cur)
            cur = []
    if cur:
        groups.append(cur)

    for group in groups:
        g_max = max(len(t) for t, _, _ in group)
        for type_str, name_str, doc in group:
            w.line(f"    {type_str.ljust(g_max)} {name_str}")
            if doc:
                w.comment(_norm_doc(doc), indent=4, max_width=79)


# ---------------------------------------------------------------------------
# Wrapping helpers (inline section)
# ---------------------------------------------------------------------------


def _emit_paren_wrap(w: Writer, line: str, max_width: int = 79) -> None:
    """Emit a function declaration preferring paren wrapping over return-type split.

    Bloomberg's codegen keeps the return type on the same line as the
    function name and wraps at '(' with 4-space continuation indent.
    Falls back to ``w.wrapped()`` when paren wrapping doesn't fit.
    """
    if len(line) <= max_width:
        w.line(line)
        return

    # Find first top-level '('
    paren_pos = None
    angle = 0
    i = 0
    while i < len(line):
        ch = line[i]
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
        w.wrapped(line)
        return

    # Find matching ')'
    prefix = line[: paren_pos + 1]
    rest = line[paren_pos + 1 :]
    depth = 1
    close_pos = None
    for j, ch in enumerate(rest):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                close_pos = j
                break

    if close_pos is None:
        w.wrapped(line)
        return

    params_str = rest[:close_pos].strip()
    suffix = rest[close_pos:]

    stripped = line.lstrip()
    base_indent = len(line) - len(stripped)
    cont_indent = " " * (base_indent + 4)
    wrapped_line = cont_indent + params_str + suffix

    if len(prefix) <= max_width and len(wrapped_line) <= max_width:
        w.line(prefix)
        w.line(wrapped_line)
    else:
        w.wrapped(line)


def _emit_info_array_call(w: Writer, line: str, max_width: int = 79) -> None:
    """Emit a function call using paren-aligned wrapping.

    Bloomberg wraps calls like ``manipulator(&member, INFO_ARRAY[INDEX])``
    with continuation aligned after '(', further splitting at '[' if needed.
    Falls back to ``w.wrapped()`` when paren-aligned wrapping doesn't fit.
    """
    if len(line) <= max_width:
        w.line(line)
        return

    # Find the opening '('
    paren_pos = None
    for i, ch in enumerate(line):
        if ch == "(":
            paren_pos = i
            break

    if paren_pos is None:
        w.wrapped(line)
        return

    # Find matching ')'
    depth = 0
    close_pos = None
    for i in range(paren_pos, len(line)):
        if line[i] == "(":
            depth += 1
        elif line[i] == ")":
            depth -= 1
            if depth == 0:
                close_pos = i
                break

    if close_pos is None:
        w.wrapped(line)
        return

    prefix = line[: paren_pos + 1]
    inside = line[paren_pos + 1 : close_pos]
    suffix = line[close_pos:]

    # Find top-level comma
    comma_pos = None
    d_a = 0
    d_p = 0
    for i, ch in enumerate(inside):
        if ch == "<":
            d_a += 1
        elif ch == ">":
            d_a -= 1
        elif ch == "(":
            d_p += 1
        elif ch == ")":
            d_p -= 1
        elif ch == "," and d_a == 0 and d_p == 0:
            comma_pos = i
            break

    if comma_pos is None:
        w.wrapped(line)
        return

    first_arg = inside[:comma_pos].strip()
    second_arg = inside[comma_pos + 1 :].strip()

    cont = " " * len(prefix)
    first_line = prefix + first_arg + ","
    second_full = cont + second_arg + suffix

    if len(first_line) <= max_width and len(second_full) <= max_width:
        w.line(first_line)
        w.line(second_full)
        return

    if len(first_line) <= max_width and "[" in second_arg:
        bracket_pos = second_arg.index("[")
        array_part = second_arg[:bracket_pos]
        index_part = second_arg[bracket_pos:]
        sub_indent = cont + "    "

        array_line = cont + array_part
        index_line = sub_indent + index_part + suffix

        if len(array_line) <= max_width and len(index_line) <= max_width:
            w.line(first_line)
            w.line(array_line)
            w.line(index_line)
            return

    w.wrapped(line)


def _emit_bsls_assert(w: Writer, line: str, max_width: int = 79) -> None:
    """Emit BSLS_ASSERT with paren-aligned wrapping at ``==`` boundary.

    Bloomberg wraps long ``BSLS_ASSERT(expr == rhs)`` calls by breaking
    at ``==`` with continuation aligned after ``(``.  Falls back to
    ``w.wrapped()`` when this layout doesn't fit.
    """
    if len(line) <= max_width:
        w.line(line)
        return

    idx = line.find("BSLS_ASSERT(")
    if idx < 0:
        w.wrapped(line)
        return

    paren_col = idx + len("BSLS_ASSERT")  # position of '('
    prefix = line[: paren_col + 1]
    rest = line[paren_col + 1 :]

    # Find closing ')'
    depth = 1
    close = None
    for i, ch in enumerate(rest):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                close = i
                break

    if close is None:
        w.wrapped(line)
        return

    expr = rest[:close]
    tail = rest[close:]  # ); and anything after

    eq = expr.find(" == ")
    if eq < 0:
        w.wrapped(line)
        return

    left = expr[: eq + 3]  # include " =="
    right = expr[eq + 4 :]  # skip " == "
    cont = " " * (paren_col + 1)

    line1 = prefix + left
    line2 = cont + right.lstrip() + tail

    if len(line1) <= max_width and len(line2) <= max_width:
        w.line(line1)
        w.line(line2)
    else:
        w.wrapped(line)


# ---------------------------------------------------------------------------
# Enum emission
# ---------------------------------------------------------------------------


def _emit_enum_decl(w: Writer, enum: EnumType, pkg: str) -> None:
    """Emit struct declaration for an enumeration type."""
    name = to_class_name(enum.name)
    w.line()
    w.banner(name)
    w.line()
    w.line(f"struct {name} {{")
    if enum.doc:
        _emit_class_doc(w, enum.doc)
        w.line()
    w.line("  public:")
    w.line("    // TYPES")
    # Build enum values: e_-prefixed primaries + aliases
    aliases = [(to_enum_value(v.name), v.id) for v in enum.values]
    e_values = [(f"e_{a}", id_) for a, id_ in aliases]

    # Alignment widths
    max_e_len = max(len(e) for e, _ in e_values)
    max_alias_len = max(len(a) for a, _ in aliases)

    w.line("    enum Value {")
    # Primary e_-prefixed values (all have trailing commas)
    for e_name, id_ in e_values:
        w.line(f"        {e_name.ljust(max_e_len)} = {id_},")
    w.line()
    # Aliases (no comma on last)
    for i, ((alias_name, _), (e_name, _)) in enumerate(zip(aliases, e_values)):
        comma = "," if i < len(aliases) - 1 else ""
        w.line(f"        {alias_name.ljust(max_alias_len)} = {e_name}{comma}")
    w.line("    };")
    w.line()
    w.line(
        f"    enum {{ k_NUM_ENUMERATORS = {len(enum.values)}, NUM_ENUMERATORS = k_NUM_ENUMERATORS }};"
    )
    w.line()
    w.line("    // CONSTANTS")
    w.line("    static const char CLASS_NAME[];")
    w.line()
    w.line("    static const bdlat_EnumeratorInfo ENUMERATOR_INFO_ARRAY[];")
    w.line()
    w.line("    // CLASS METHODS")
    w.line("    static const char* toString(Value value);")
    w.line("    // Return the string representation exactly matching the enumerator")
    w.line("    // name corresponding to the specified enumeration 'value'.")
    w.line()
    w.line(
        "    static int fromString(Value* result, const char* string, int stringLength);"
    )
    w.line("    // Load into the specified 'result' the enumerator matching the")
    w.line("    // specified 'string' of the specified 'stringLength'.  Return 0 on")
    w.line("    // success, and a non-zero value with no effect on 'result' otherwise")
    w.line("    // (i.e., 'string' does not match any enumerator).")
    w.line()
    w.line("    static int fromString(Value* result, const bsl::string& string);")
    w.line("    // Load into the specified 'result' the enumerator matching the")
    w.line("    // specified 'string'.  Return 0 on success, and a non-zero value with")
    w.line("    // no effect on 'result' otherwise (i.e., 'string' does not match any")
    w.line("    // enumerator).")
    w.line()
    w.line("    static int fromInt(Value* result, int number);")
    w.line("    // Load into the specified 'result' the enumerator matching the")
    w.line("    // specified 'number'.  Return 0 on success, and a non-zero value with")
    w.line("    // no effect on 'result' otherwise (i.e., 'number' does not match any")
    w.line("    // enumerator).")
    w.line()
    w.line("    static bsl::ostream& print(bsl::ostream& stream, Value value);")
    w.line("    // Write to the specified 'stream' the string representation of")
    w.line("    // the specified enumeration 'value'.  Return a reference to")
    w.line("    // the modifiable 'stream'.")
    w.line()
    w.line("    // HIDDEN FRIENDS")
    w.line("    friend bsl::ostream& operator<<(bsl::ostream& stream, Value rhs)")
    w.line("    // Format the specified 'rhs' to the specified output 'stream' and")
    w.line("    // return a reference to the modifiable 'stream'.")
    w.line("    {")
    w.line(f"        return {name}::print(stream, rhs);")
    w.line("    }")
    w.line("};")
    w.line()
    w.line("}  // close package namespace")
    w.line()
    w.line("// TRAITS")
    w.line()
    w.line(f"BDLAT_DECL_ENUMERATION_TRAITS({pkg}::{name});")


# ---------------------------------------------------------------------------
# Sequence emission
# ---------------------------------------------------------------------------


def _emit_sequence_decl(w: Writer, t: ComplexType, schema: Schema, pkg: str) -> None:
    """Emit class declaration for a sequence type."""
    name = to_class_name(t.name)
    has_alloc = type_needs_allocator(t, schema)
    resolved = [resolve_field(f, t, schema) for f in t.fields]
    sorted_fields = sort_fields_by_alignment(t.fields, schema, t)
    sorted_resolved = [resolve_field(f, t, schema) for f in sorted_fields]
    n = len(t.fields)
    use_hash_helper = n >= 3
    use_eq_helper = n >= 4

    w.line()
    w.banner(name)
    w.line()
    w.line(f"class {name} {{")
    if t.doc:
        _emit_class_doc(w, t.doc)
        w.line()

    # INSTANCE DATA
    w.line("    // INSTANCE DATA")
    has_pointer_fields = any(_needs_pointer_storage(f, schema) for f in t.fields)

    # Build member declarations for column alignment
    # (type_str, name_str, doc_or_none)
    _members: list[tuple[str, str, str | None]] = []
    if has_pointer_fields:
        _members.append(("bslma::Allocator*", "d_allocator_p;", None))
    for rf in sorted_resolved:
        if _needs_pointer_storage(rf.field, schema):
            _members.append(
                (f"{rf.cpp_type}*", f"{to_member_name(rf.field.name)};", rf.field.doc)
            )
        else:
            _members.append(
                (rf.decl_type, f"{to_member_name(rf.field.name)};", rf.field.doc)
            )

    if _members:
        _emit_aligned_seq_members(w, _members)

    # PRIVATE ACCESSORS (if needed)
    if use_hash_helper or use_eq_helper:
        w.line()
        w.line("    // PRIVATE ACCESSORS")
        if use_hash_helper:
            w.line("    template <typename t_HASH_ALGORITHM>")
            w.line("    void hashAppendImpl(t_HASH_ALGORITHM& hashAlgorithm) const;")
        if use_eq_helper:
            w.line()
            w.line(f"    bool isEqualTo(const {name}& rhs) const;")

    w.line()
    w.line("  public:")
    w.line("    // TYPES")

    # ATTRIBUTE_ID and INDEX enums (only if there are fields)
    if n > 0:
        _emit_attr_id_enum(w, t.fields)
        w.line()
    w.line(f"    enum {{ NUM_ATTRIBUTES = {n} }};")
    if n > 0:
        w.line()
        _emit_attr_index_enum(w, t.fields)
    w.line()

    # CONSTANTS
    w.line("    // CONSTANTS")
    w.line("    static const char CLASS_NAME[];")
    # DEFAULT_INITIALIZER declarations for fields with defaults
    for rf in resolved:
        if rf.field.default is not None:
            const_name = f"DEFAULT_INITIALIZER_{to_upper_snake(rf.field.name)}"
            w.line()
            if rf.field.type_name == "xs:string":
                w.line(f"    static const char {const_name}[];")
            else:
                w.line(f"    static const {rf.cpp_type} {const_name};")
    if n > 0:
        w.line()
        w.line("    static const bdlat_AttributeInfo ATTRIBUTE_INFO_ARRAY[];")
    w.line()

    # CLASS METHODS
    w.line("  public:")
    w.line("    // CLASS METHODS")
    w.line("    static const bdlat_AttributeInfo* lookupAttributeInfo(int id);")
    w.line("    // Return attribute information for the attribute indicated by the")
    w.line("    // specified 'id' if the attribute exists, and 0 otherwise.")
    w.line()
    w.wrapped(
        "    static const bdlat_AttributeInfo* lookupAttributeInfo(const char* name, int nameLength);"
    )
    w.line("    // Return attribute information for the attribute indicated by the")
    w.line("    // specified 'name' of the specified 'nameLength' if the attribute")
    w.line("    // exists, and 0 otherwise.")
    w.line()

    # CREATORS
    w.line("    // CREATORS")
    if has_alloc:
        w.wrapped(f"    explicit {name}(bslma::Allocator* basicAllocator = 0);")
        w.comment(
            f"Create an object of type '{name}' having the default value.  Use the optionally specified 'basicAllocator' to supply memory.  If 'basicAllocator' is 0, the currently installed default allocator is used."
        )
        w.line()
        w.wrapped(
            f"    {name}(const {name}& original, bslma::Allocator* basicAllocator = 0);"
        )
        w.comment(
            f"Create an object of type '{name}' having the value of the specified 'original' object.  Use the optionally specified 'basicAllocator' to supply memory.  If 'basicAllocator' is 0, the currently installed default allocator is used."
        )
        w.line()
        w.line(
            "#if defined(BSLS_COMPILERFEATURES_SUPPORT_RVALUE_REFERENCES) &&               \\"
        )
        w.line("    defined(BSLS_COMPILERFEATURES_SUPPORT_NOEXCEPT)")
        w.wrapped(f"    {name}({name}&& original) noexcept;")
        w.comment(
            f"Create an object of type '{name}' having the value of the specified 'original' object.  After performing this action, the 'original' object will be left in a valid, but unspecified state."
        )
        w.line()
        w.wrapped(f"    {name}({name}&& original, bslma::Allocator* basicAllocator);")
        w.comment(
            f"Create an object of type '{name}' having the value of the specified 'original' object.  After performing this action, the 'original' object will be left in a valid, but unspecified state.  Use the optionally specified 'basicAllocator' to supply memory.  If 'basicAllocator' is 0, the currently installed default allocator is used."
        )
        w.line("#endif")
        w.line()
        w.line(f"    ~{name}();")
        w.comment("Destroy this object.")
    elif t.fields:
        # Non-allocator types with fields get an explicit default constructor
        w.line(f"    {name}();")
        w.comment(f"Create an object of type '{name}' having the default value.")
    # For non-allocator empty types, no explicit constructor is emitted
    w.line()

    # MANIPULATORS
    w.line("    // MANIPULATORS")
    if has_alloc:
        w.wrapped(f"    {name}& operator=(const {name}& rhs);")
        w.comment("Assign to this object the value of the specified 'rhs' object.")
        w.line()
        w.line(
            "#if defined(BSLS_COMPILERFEATURES_SUPPORT_RVALUE_REFERENCES) &&               \\"
        )
        w.line("    defined(BSLS_COMPILERFEATURES_SUPPORT_NOEXCEPT)")
        w.wrapped(f"    {name}& operator=({name}&& rhs);")
        w.comment(
            "Assign to this object the value of the specified 'rhs' object.  After performing this action, the 'rhs' object will be left in a valid, but unspecified state."
        )
        w.line("#endif")
        w.line()

    w.line("    void reset();")
    w.line("    // Reset this object to the default value (i.e., its value upon")
    w.line("    // default construction).")
    w.line()
    w.line("    template <typename t_MANIPULATOR>")
    w.line("    int manipulateAttributes(t_MANIPULATOR& manipulator);")
    w.line("    // Invoke the specified 'manipulator' sequentially on the address of")
    w.line("    // each (modifiable) attribute of this object, supplying 'manipulator'")
    w.line("    // with the corresponding attribute information structure until such")
    w.line("    // invocation returns a non-zero value.  Return the value from the")
    w.line("    // last invocation of 'manipulator' (i.e., the invocation that")
    w.line("    // terminated the sequence).")
    w.line()
    w.line("    template <typename t_MANIPULATOR>")
    w.line("    int manipulateAttribute(t_MANIPULATOR& manipulator, int id);")
    w.line("    // Invoke the specified 'manipulator' on the address of")
    w.line("    // the (modifiable) attribute indicated by the specified 'id',")
    w.line("    // supplying 'manipulator' with the corresponding attribute")
    w.line("    // information structure.  Return the value returned from the")
    w.line("    // invocation of 'manipulator' if 'id' identifies an attribute of this")
    w.line("    // class, and -1 otherwise.")
    w.line()
    w.line("    template <typename t_MANIPULATOR>")
    w.wrapped(
        "    int manipulateAttribute(t_MANIPULATOR& manipulator, const char* name, int nameLength);"
    )
    w.line("    // Invoke the specified 'manipulator' on the address of")
    w.line("    // the (modifiable) attribute indicated by the specified 'name' of the")
    w.line("    // specified 'nameLength', supplying 'manipulator' with the")
    w.line("    // corresponding attribute information structure.  Return the value")
    w.line("    // returned from the invocation of 'manipulator' if 'name' identifies")
    w.line("    // an attribute of this class, and -1 otherwise.")
    w.line()

    # Mutable accessors (manipulators for each field)
    for rf in resolved:
        mut_type, _ = _accessor_return(rf)
        w.wrapped(f"    {mut_type} {rf.name}();")
        w.comment(
            f'Return a reference to the modifiable "{_title_case(rf.field.name)}" attribute of this object.'
        )
        w.line()

    # ACCESSORS
    w.line("    // ACCESSORS")
    w.line("    bsl::ostream&")
    w.line(
        "    print(bsl::ostream& stream, int level = 0, int spacesPerLevel = 4) const;"
    )
    w.line("    // Format this object to the specified output 'stream' at the")
    w.line("    // optionally specified indentation 'level' and return a reference to")
    w.line("    // the modifiable 'stream'.  If 'level' is specified, optionally")
    w.line(
        "    // specify 'spacesPerLevel', the number of spaces per indentation level"
    )
    w.line("    // for this and all of its nested objects.  Each line is indented by")
    w.line("    // the absolute value of 'level * spacesPerLevel'.  If 'level' is")
    w.line("    // negative, suppress indentation of the first line.  If")
    w.line("    // 'spacesPerLevel' is negative, suppress line breaks and format the")
    w.line("    // entire output on one line.  If 'stream' is initially invalid, this")
    w.line("    // operation has no effect.  Note that a trailing newline is provided")
    w.line("    // in multiline mode only.")
    w.line()
    w.line("    template <typename t_ACCESSOR>")
    w.line("    int accessAttributes(t_ACCESSOR& accessor) const;")
    w.line("    // Invoke the specified 'accessor' sequentially on each")
    w.line("    // (non-modifiable) attribute of this object, supplying 'accessor'")
    w.line("    // with the corresponding attribute information structure until such")
    w.line("    // invocation returns a non-zero value.  Return the value from the")
    w.line("    // last invocation of 'accessor' (i.e., the invocation that terminated")
    w.line("    // the sequence).")
    w.line()
    w.line("    template <typename t_ACCESSOR>")
    w.line("    int accessAttribute(t_ACCESSOR& accessor, int id) const;")
    w.line("    // Invoke the specified 'accessor' on the (non-modifiable) attribute")
    w.line(
        "    // of this object indicated by the specified 'id', supplying 'accessor'"
    )
    w.line("    // with the corresponding attribute information structure.  Return the")
    w.line("    // value returned from the invocation of 'accessor' if 'id' identifies")
    w.line("    // an attribute of this class, and -1 otherwise.")
    w.line()
    w.line("    template <typename t_ACCESSOR>")
    w.wrapped(
        "    int accessAttribute(t_ACCESSOR& accessor, const char* name, int nameLength) const;"
    )
    w.line("    // Invoke the specified 'accessor' on the (non-modifiable) attribute")
    w.line("    // of this object indicated by the specified 'name' of the specified")
    w.line("    // 'nameLength', supplying 'accessor' with the corresponding attribute")
    w.line("    // information structure.  Return the value returned from the")
    w.line("    // invocation of 'accessor' if 'name' identifies an attribute of this")
    w.line("    // class, and -1 otherwise.")
    w.line()

    # Const accessors for each field
    for rf in resolved:
        _, const_type = _accessor_return(rf)
        w.wrapped(f"    {const_type} {rf.name}() const;")
        title = _title_case(rf.field.name)
        if const_type.startswith("const "):
            w.comment(
                f'Return a reference offering non-modifiable access to the "{title}" attribute of this object.'
            )
        else:
            w.comment(f'Return the value of the "{title}" attribute of this object.')
        w.line()

    # HIDDEN FRIENDS
    w.line("    // HIDDEN FRIENDS")
    _emit_operator_eq(w, name, resolved, use_eq_helper)
    w.line()
    w.wrapped(f"    friend bool operator!=(const {name}& lhs, const {name}& rhs)")
    w.line("    // Returns '!(lhs == rhs)'")
    w.line("    {")
    w.line("        return !(lhs == rhs);")
    w.line("    }")
    w.line()
    w.wrapped(
        f"    friend bsl::ostream& operator<<(bsl::ostream& stream, const {name}& rhs)"
    )
    w.line("    // Format the specified 'rhs' to the specified output 'stream' and")
    w.line("    // return a reference to the modifiable 'stream'.")
    w.line("    {")
    w.line("        return rhs.print(stream, 0, -1);")
    w.line("    }")
    w.line()
    _emit_hash_append(w, name, resolved, use_hash_helper)
    w.line("};")
    w.line()
    w.line("}  // close package namespace")
    w.line()
    w.line("// TRAITS")
    w.line()
    if has_alloc:
        w.wrapped(
            f"BDLAT_DECL_SEQUENCE_WITH_ALLOCATOR_BITWISEMOVEABLE_TRAITS({pkg}::{name});"
        )
    else:
        w.wrapped(f"BDLAT_DECL_SEQUENCE_WITH_BITWISEMOVEABLE_TRAITS({pkg}::{name});")
    w.line("template <>")
    _emit_uses_default_value_flag(w, pkg, name)


def _emit_uses_default_value_flag(w: Writer, pkg: str, name: str) -> None:
    """Emit the bdlat_UsesDefaultValueFlag specialization, wrapping if needed."""
    full = f"struct bdlat_UsesDefaultValueFlag<{pkg}::{name}> : bsl::true_type {{}};"
    if len(full) <= 79:
        w.line(full)
    else:
        # Try brace split first (clang-format preference: keep as much on
        # one line as possible, break at '{' when just barely over 79).
        brace_line = (
            f"struct bdlat_UsesDefaultValueFlag<{pkg}::{name}> : bsl::true_type {{"
        )
        if len(brace_line) <= 79:
            w.line(brace_line)
            w.line("};")
        else:
            w.line(f"struct bdlat_UsesDefaultValueFlag<{pkg}::{name}>")
            w.line(": bsl::true_type {};")


def _emit_aligned_enum(w: Writer, entries: list[tuple[str, int | str]]) -> None:
    """Emit an aligned enum block.  *entries* is [(NAME, value), ...].

    If everything fits on one ``enum { ... };`` line (≤79 chars at 4-space
    indent), emit on a single line.  Otherwise emit multi-line with
    column-aligned ``=`` signs (matching clang-format
    ``AlignConsecutiveAssignments``).
    """
    # Try one-line form
    parts = ", ".join(f"{n} = {v}" for n, v in entries)
    one_line = f"    enum {{ {parts} }};"
    if len(one_line) <= 79:
        w.line(one_line)
        return

    # Multi-line with alignment
    max_name = max(len(n) for n, _ in entries)
    w.line("    enum {")
    for i, (name, val) in enumerate(entries):
        comma = "," if i < len(entries) - 1 else ""
        w.line(f"        {name.ljust(max_name)} = {val}{comma}")
    w.line("    };")


def _emit_attr_id_enum(w: Writer, fields: list[Field]) -> None:
    """Emit the ATTRIBUTE_ID enum, using bdem:id when present."""
    entries = [
        (f"ATTRIBUTE_ID_{to_upper_snake(f.name)}", f.id if f.id is not None else i)
        for i, f in enumerate(fields)
    ]
    _emit_aligned_enum(w, entries)


def _emit_attr_index_enum(w: Writer, fields: list[Field]) -> None:
    """Emit the ATTRIBUTE_INDEX enum."""
    entries = [
        (f"ATTRIBUTE_INDEX_{to_upper_snake(f.name)}", i) for i, f in enumerate(fields)
    ]
    _emit_aligned_enum(w, entries)


def _emit_operator_eq(
    w: Writer, name: str, resolved: list[ResolvedField], use_helper: bool
) -> None:
    """Emit operator== friend."""
    if not resolved:
        # 0-field type: unnamed params
        w.wrapped(f"    friend bool operator==(const {name}&, const {name}&)")
        w.line(
            "    // Returns 'true' as this type has no attributes and so all objects of"
        )
        w.line("    // this type are considered equal.")
        w.line("    {")
        w.line("        return true;")
    elif use_helper:
        w.wrapped(f"    friend bool operator==(const {name}& lhs, const {name}& rhs)")
        w.line(
            "    // Return 'true' if the specified 'lhs' and 'rhs' attribute objects"
        )
        w.line(
            "    // have the same value, and 'false' otherwise.  Two attribute objects"
        )
        w.line(
            "    // have the same value if each respective attribute has the same value."
        )
        w.line("    {")
        w.line("        return lhs.isEqualTo(rhs);")
    else:
        w.wrapped(f"    friend bool operator==(const {name}& lhs, const {name}& rhs)")
        w.line(
            "    // Return 'true' if the specified 'lhs' and 'rhs' attribute objects"
        )
        w.line(
            "    // have the same value, and 'false' otherwise.  Two attribute objects"
        )
        w.line(
            "    // have the same value if each respective attribute has the same value."
        )
        w.line("    {")
        parts = [f"lhs.{rf.name}() == rhs.{rf.name}()" for rf in resolved]
        _emit_comparison_chain(
            w,
            parts,
            prefix="        return ",
            cont_indent="               ",
            suffix=";",
        )
    w.line("    }")


def _emit_hash_append(
    w: Writer, name: str, resolved: list[ResolvedField], use_helper: bool
) -> None:
    """Emit hashAppend friend."""
    w.line("    template <typename t_HASH_ALGORITHM>")
    if not resolved:
        w.wrapped(f"    friend void hashAppend(t_HASH_ALGORITHM&, const {name}&)")
    else:
        w.wrapped(
            f"    friend void hashAppend(t_HASH_ALGORITHM& hashAlg, const {name}& object)"
        )
    w.comment(
        f"Pass the specified 'object' to the specified 'hashAlg'.  This function integrates with the 'bslh' modular hashing system and effectively provides a 'bsl::hash' specialization for '{name}'."
    )
    w.line("    {")
    if not resolved:
        pass  # Empty body
    elif use_helper:
        w.line("        object.hashAppendImpl(hashAlg);")
    else:
        w.line("        using bslh::hashAppend;")
        for rf in resolved:
            w.line(f"        hashAppend(hashAlg, object.{rf.name}());")
    w.line("    }")


def _title_case(name: str) -> str:
    """Convert camelCase to TitleCase for doc comments (e.g. 'asJSON' -> 'AsJSON').

    Handles hyphenated names: 'batch-post' -> 'BatchPost'.
    """
    from .naming import to_title_name  # pylint: disable=import-outside-toplevel

    return to_title_name(name)


def _emit_comparison_chain(  # pylint: disable=too-many-positional-arguments
    w: Writer,
    parts: list[str],
    prefix: str,
    cont_indent: str,
    suffix: str,
    max_width: int = 79,
) -> None:
    """Emit a chained equality comparison (``lhs.x() == rhs.x() && ...``).

    Parts are packed greedily onto lines (matching bas_codegen.pl output).
    When a line exceeds *max_width*, it is wrapped at ``==``.
    """
    # Phase 1: greedily pack parts into lines
    segments: list[tuple[str, str]] = []  # (text, trailing)
    current = prefix + parts[0]
    for i in range(1, len(parts)):
        is_last = i == len(parts) - 1
        candidate = current + " && " + parts[i]
        trailing = suffix if is_last else " &&"
        if len(candidate + trailing) <= max_width:
            current = candidate
        else:
            segments.append((current, " &&"))
            current = cont_indent + parts[i]
    segments.append((current, suffix))

    # Phase 2: emit each segment, splitting at " == " when overlong
    for i, (text, trailing) in enumerate(segments):
        full = text + trailing
        if len(full) <= max_width:
            w.line(full)
        else:
            eq_idx = text.rfind(" == ")
            if eq_idx >= 0:
                w.line(text[:eq_idx] + " ==")
                # Extra 4-space indent unless this is the single/first
                # comparison on the prefix line with no further "&&".
                text_indent = len(text) - len(text.lstrip())
                if text_indent >= len(cont_indent) or trailing.strip() == "&&":
                    wrap = cont_indent + "    "
                else:
                    wrap = cont_indent
                w.line(wrap + text[eq_idx + 4 :] + trailing)
            else:
                w.line(full)  # Can't split — emit as-is


# ---------------------------------------------------------------------------
# Choice emission
# ---------------------------------------------------------------------------


def _choice_needs_allocator(t: ComplexType, schema: Schema) -> bool:
    """Determine if a choice type needs an allocator."""
    for f in t.choices:
        if f.type_name in XSD_TYPE_MAP:
            cpp, _, _ = XSD_TYPE_MAP[f.type_name]
            if cpp == "bsl::string":
                return True
        else:
            ref_name = f.type_name.removeprefix("tns:")
            resolved = schema.type_by_name(ref_name)
            if isinstance(resolved, ComplexType) and type_needs_allocator(
                resolved, schema
            ):
                return True
    return False


def _emit_choice_decl(w: Writer, t: ComplexType, schema: Schema, pkg: str) -> None:
    """Emit class declaration for a choice type."""
    name = to_class_name(t.name)
    has_alloc = _choice_needs_allocator(t, schema)
    resolved = [resolve_field(f, t, schema) for f in t.choices]
    n = len(t.choices)

    w.line()
    w.banner(name)
    w.line()
    w.line(f"class {name} {{")
    if t.doc:
        _emit_class_doc(w, t.doc)
        w.line()

    # INSTANCE DATA — union
    w.line("    // INSTANCE DATA")
    w.line("    union {")
    # Column-align ObjectBuffer declarations, with doc-comment group breaks
    # and overflow wrapping (when padded line would exceed 79 chars).
    ob_types = [f"bsls::ObjectBuffer<{rf.cpp_type}>" for rf in resolved]
    _emit_aligned_union_members(w, resolved, ob_types)
    w.line("    };")
    w.line()
    if has_alloc:
        # Column-align int with bslma::Allocator*
        alloc_type = "bslma::Allocator*"
        w.line(f"    {'int'.ljust(len(alloc_type))} d_selectionId;")
        w.line(f"    {alloc_type} d_allocator_p;")
    else:
        w.line("    int d_selectionId;")
    w.line()

    # PRIVATE ACCESSORS (always for choices)
    w.line("    // PRIVATE ACCESSORS")
    w.line("    template <typename t_HASH_ALGORITHM>")
    w.line("    void hashAppendImpl(t_HASH_ALGORITHM& hashAlgorithm) const;")
    w.line()
    w.line(f"    bool isEqualTo(const {name}& rhs) const;")
    w.line()
    w.line("  public:")
    w.line("    // TYPES")
    w.line()

    # SELECTION_ID enum
    sel_entries: list[tuple[str, int | str]] = [("SELECTION_ID_UNDEFINED", -1)]
    for i, f in enumerate(t.choices):
        sel_entries.append(
            (f"SELECTION_ID_{to_upper_snake(f.name)}", f.id if f.id is not None else i)
        )
    _emit_aligned_enum(w, sel_entries)
    w.line()
    w.line(f"    enum {{ NUM_SELECTIONS = {n} }};")
    w.line()

    # SELECTION_INDEX enum
    idx_entries = [
        (f"SELECTION_INDEX_{to_upper_snake(f.name)}", i)
        for i, f in enumerate(t.choices)
    ]
    _emit_aligned_enum(w, idx_entries)
    w.line()

    # CONSTANTS
    w.line("    // CONSTANTS")
    w.line("    static const char CLASS_NAME[];")
    w.line()
    w.line("    static const bdlat_SelectionInfo SELECTION_INFO_ARRAY[];")
    w.line()

    # CLASS METHODS
    w.line("    // CLASS METHODS")
    w.line("    static const bdlat_SelectionInfo* lookupSelectionInfo(int id);")
    w.line("    // Return selection information for the selection indicated by the")
    w.line("    // specified 'id' if the selection exists, and 0 otherwise.")
    w.line()
    w.wrapped(
        "    static const bdlat_SelectionInfo* lookupSelectionInfo(const char* name, int nameLength);"
    )
    w.line("    // Return selection information for the selection indicated by the")
    w.line("    // specified 'name' of the specified 'nameLength' if the selection")
    w.line("    // exists, and 0 otherwise.")
    w.line()

    # CREATORS
    w.line("    // CREATORS")
    if has_alloc:
        w.wrapped(f"    explicit {name}(bslma::Allocator* basicAllocator = 0);")
        w.comment(
            f"Create an object of type '{name}' having the default value.  Use the optionally specified 'basicAllocator' to supply memory.  If 'basicAllocator' is 0, the currently installed default allocator is used."
        )
        w.line()
        w.wrapped(
            f"    {name}(const {name}& original, bslma::Allocator* basicAllocator = 0);"
        )
        w.comment(
            f"Create an object of type '{name}' having the value of the specified 'original' object.  Use the optionally specified 'basicAllocator' to supply memory.  If 'basicAllocator' is 0, the currently installed default allocator is used."
        )
        w.line()
        w.line(
            "#if defined(BSLS_COMPILERFEATURES_SUPPORT_RVALUE_REFERENCES) &&               \\"
        )
        w.line("    defined(BSLS_COMPILERFEATURES_SUPPORT_NOEXCEPT)")
        w.wrapped(f"    {name}({name}&& original) noexcept;")
        w.comment(
            f"Create an object of type '{name}' having the value of the specified 'original' object.  After performing this action, the 'original' object will be left in a valid, but unspecified state."
        )
        w.line()
        w.wrapped(f"    {name}({name}&& original, bslma::Allocator* basicAllocator);")
        w.comment(
            f"Create an object of type '{name}' having the value of the specified 'original' object.  After performing this action, the 'original' object will be left in a valid, but unspecified state.  Use the optionally specified 'basicAllocator' to supply memory.  If 'basicAllocator' is 0, the currently installed default allocator is used."
        )
        w.line("#endif")
    else:
        w.wrapped(f"    {name}();")
        w.comment(f"Create an object of type '{name}' having the default value.")
        w.line()
        w.wrapped(f"    {name}(const {name}& original);")
        w.comment(
            f"Create an object of type '{name}' having the value of the specified 'original' object."
        )
        w.line()
        w.line(
            "#if defined(BSLS_COMPILERFEATURES_SUPPORT_RVALUE_REFERENCES) &&               \\"
        )
        w.line("    defined(BSLS_COMPILERFEATURES_SUPPORT_NOEXCEPT)")
        w.wrapped(f"    {name}({name}&& original) noexcept;")
        w.comment(
            f"Create an object of type '{name}' having the value of the specified 'original' object.  After performing this action, the 'original' object will be left in a valid, but unspecified state."
        )
        w.line("#endif")
    w.line()
    w.line(f"    ~{name}();")
    w.comment("Destroy this object.")
    w.line()

    # MANIPULATORS
    w.line("    // MANIPULATORS")
    w.wrapped(f"    {name}& operator=(const {name}& rhs);")
    w.line("    // Assign to this object the value of the specified 'rhs' object.")
    w.line()
    w.line(
        "#if defined(BSLS_COMPILERFEATURES_SUPPORT_RVALUE_REFERENCES) &&               \\"
    )
    w.line("    defined(BSLS_COMPILERFEATURES_SUPPORT_NOEXCEPT)")
    w.wrapped(f"    {name}& operator=({name}&& rhs);")
    w.line("    // Assign to this object the value of the specified 'rhs' object.")
    w.line("    // After performing this action, the 'rhs' object will be left in a")
    w.line("    // valid, but unspecified state.")
    w.line("#endif")
    w.line()
    w.line("    void reset();")
    w.line(
        "    // Reset this object to the default value (i.e., its value upon default"
    )
    w.line("    // construction).")
    w.line()
    w.line("    int makeSelection(int selectionId);")
    w.line("    // Set the value of this object to be the default for the selection")
    w.line("    // indicated by the specified 'selectionId'.  Return 0 on success, and")
    w.line("    // non-zero value otherwise (i.e., the selection is not found).")
    w.line()
    w.wrapped("    int makeSelection(const char* name, int nameLength);")
    w.line("    // Set the value of this object to be the default for the selection")
    w.line("    // indicated by the specified 'name' of the specified 'nameLength'.")
    w.line("    // Return 0 on success, and non-zero value otherwise (i.e., the")
    w.line("    // selection is not found).")
    w.line()

    # make<Name>() methods
    for rf in resolved:
        fname = rf.field.name
        title = _title_case(fname)
        is_prim = _field_is_primitive(rf)
        w.wrapped(f"    {rf.cpp_type}& make{title}();")
        if is_prim:
            # Primitives pass by value, no rvalue overload
            w.wrapped(f"    {rf.cpp_type}& make{title}({rf.cpp_type} value);")
        else:
            w.wrapped(f"    {rf.cpp_type}& make{title}(const {rf.cpp_type}& value);")
            w.line(
                "#if defined(BSLS_COMPILERFEATURES_SUPPORT_RVALUE_REFERENCES) &&               \\"
            )
            w.line("    defined(BSLS_COMPILERFEATURES_SUPPORT_NOEXCEPT)")
            w.wrapped(f"    {rf.cpp_type}& make{title}({rf.cpp_type}&& value);")
            w.line("#endif")
        w.comment(
            f'Set the value of this object to be a "{title}" value.  Optionally specify the \'value\' of the "{title}".  If \'value\' is not specified, the default "{title}" value is used.'
        )
        w.line()

    # manipulateSelection
    w.line("    template <typename t_MANIPULATOR>")
    w.line("    int manipulateSelection(t_MANIPULATOR& manipulator);")
    w.line("    // Invoke the specified 'manipulator' on the address of the modifiable")
    w.line("    // selection, supplying 'manipulator' with the corresponding selection")
    w.line("    // information structure.  Return the value returned from the")
    w.line("    // invocation of 'manipulator' if this object has a defined selection,")
    w.line("    // and -1 otherwise.")
    w.line()

    # Mutable accessors
    for rf in resolved:
        w.wrapped(f"    {rf.cpp_type}& {rf.name}();")
        title = _title_case(rf.field.name)
        w.comment(
            f'Return a reference to the modifiable "{title}" selection of this object if "{title}" is the current selection.  The behavior is undefined unless "{title}" is the selection of this object.'
        )
        w.line()

    # ACCESSORS
    w.line("    // ACCESSORS")
    w.line("    bsl::ostream&")
    w.line(
        "    print(bsl::ostream& stream, int level = 0, int spacesPerLevel = 4) const;"
    )
    w.line("    // Format this object to the specified output 'stream' at the")
    w.line("    // optionally specified indentation 'level' and return a reference to")
    w.line("    // the modifiable 'stream'.  If 'level' is specified, optionally")
    w.line(
        "    // specify 'spacesPerLevel', the number of spaces per indentation level"
    )
    w.line("    // for this and all of its nested objects.  Each line is indented by")
    w.line("    // the absolute value of 'level * spacesPerLevel'.  If 'level' is")
    w.line("    // negative, suppress indentation of the first line.  If")
    w.line("    // 'spacesPerLevel' is negative, suppress line breaks and format the")
    w.line("    // entire output on one line.  If 'stream' is initially invalid, this")
    w.line("    // operation has no effect.  Note that a trailing newline is provided")
    w.line("    // in multiline mode only.")
    w.line()
    w.line("    int selectionId() const;")
    w.line("    // Return the id of the current selection if the selection is defined,")
    w.line("    // and -1 otherwise.")
    w.line()
    w.line("    template <typename t_ACCESSOR>")
    w.line("    int accessSelection(t_ACCESSOR& accessor) const;")
    w.line("    // Invoke the specified 'accessor' on the non-modifiable selection,")
    w.line("    // supplying 'accessor' with the corresponding selection information")
    w.line("    // structure.  Return the value returned from the invocation of")
    w.line(
        "    // 'accessor' if this object has a defined selection, and -1 otherwise."
    )
    w.line()

    # Const accessors
    for rf in resolved:
        w.wrapped(f"    const {rf.cpp_type}& {rf.name}() const;")
        title = _title_case(rf.field.name)
        w.comment(
            f'Return a reference to the non-modifiable "{title}" selection of this object if "{title}" is the current selection.  The behavior is undefined unless "{title}" is the selection of this object.'
        )
        w.line()

    # is*Value() methods
    for rf in resolved:
        title = _title_case(rf.field.name)
        w.wrapped(f"    bool is{title}Value() const;")
        w.comment(
            f"Return 'true' if the value of this object is a \"{title}\" value, and return 'false' otherwise."
        )
        w.line()

    w.line("    bool isUndefinedValue() const;")
    w.line("    // Return 'true' if the value of this object is undefined, and 'false'")
    w.line("    // otherwise.")
    w.line()
    w.line("    const char* selectionName() const;")
    w.line("    // Return the symbolic name of the current selection of this object.")
    w.line()

    # HIDDEN FRIENDS
    w.line("    // HIDDEN FRIENDS")
    w.wrapped(f"    friend bool operator==(const {name}& lhs, const {name}& rhs)")
    w.comment(
        f"Return 'true' if the specified 'lhs' and 'rhs' objects have the same value, and 'false' otherwise.  Two '{name}' objects have the same value if either the selections in both objects have the same ids and the same values, or both selections are undefined."
    )
    w.line("    {")
    w.line("        return lhs.isEqualTo(rhs);")
    w.line("    }")
    w.line()
    w.wrapped(f"    friend bool operator!=(const {name}& lhs, const {name}& rhs)")
    w.comment(
        "Return 'true' if the specified 'lhs' and 'rhs' objects do not have the same values, as determined by 'operator==', and 'false' otherwise."
    )
    w.line("    {")
    w.line("        return !(lhs == rhs);")
    w.line("    }")
    w.line()
    w.wrapped(
        f"    friend bsl::ostream& operator<<(bsl::ostream& stream, const {name}& rhs)"
    )
    w.line("    // Format the specified 'rhs' to the specified output 'stream' and")
    w.line("    // return a reference to the modifiable 'stream'.")
    w.line("    {")
    w.line("        return rhs.print(stream, 0, -1);")
    w.line("    }")
    w.line()
    w.line("    template <typename t_HASH_ALGORITHM>")
    w.wrapped(
        f"    friend void hashAppend(t_HASH_ALGORITHM& hashAlg, const {name}& object)"
    )
    w.comment(
        f"Pass the specified 'object' to the specified 'hashAlg'.  This function integrates with the 'bslh' modular hashing system and effectively provides a 'bsl::hash' specialization for '{name}'."
    )
    w.line("    {")
    w.line("        return object.hashAppendImpl(hashAlg);")
    w.line("    }")
    w.line("};")
    w.line()
    w.line("}  // close package namespace")
    w.line()
    w.line("// TRAITS")
    w.line()
    if has_alloc:
        w.wrapped(
            f"BDLAT_DECL_CHOICE_WITH_ALLOCATOR_BITWISEMOVEABLE_TRAITS({pkg}::{name});"
        )
    else:
        w.wrapped(f"BDLAT_DECL_CHOICE_WITH_BITWISEMOVEABLE_TRAITS({pkg}::{name});")


# ---------------------------------------------------------------------------
# Top-level emission
# ---------------------------------------------------------------------------


def emit_header(
    schema: Schema,
    pkg: str,
    component: str,
    *,
    copyright: str = "",  # pylint: disable=redefined-builtin
    xsd_name: str = "",
    codegen_version: str = "",
    extra_flags: str = "",
) -> str:
    """Generate the complete .h file content.

    If *copyright* is provided it is emitted verbatim before the
    ``*DO NOT EDIT*`` banner (matching the ``codegen.sh`` pipeline).
    """
    schema = expand_hybrids(schema).schema

    w = Writer()
    component_full = f"{pkg}_{component}"
    guard = f"INCLUDED_{component_full.upper()}"

    if copyright:
        w.raw(copyright.removesuffix("\n"))
        w.line()
    w.line(make_banner(component_full, "h"))
    w.line(f"#ifndef {guard}")
    w.line(f"#define {guard}")
    w.line()
    w.line("//@PURPOSE: Provide value-semantic attribute classes")
    w.line()
    w.raw(_build_includes(schema))
    w.line()

    # Open BloombergLP namespace
    w.line("namespace BloombergLP {")
    w.line()

    # Forward declare bslma::Allocator
    w.line("namespace bslma {")
    w.line("class Allocator;")
    w.line("}")
    w.line()

    # Forward declarations (topological levels, alphabetical within)
    # Use complex-only levels for forward decls (enums don't need them)
    levels = topological_sort_types(schema)
    for level in levels:
        for type_name in level:
            w.line(f"namespace {pkg} {{")
            w.line(f"class {type_name};")
            w.line("}")

    # Type definitions — enums interleaved with classes in topo order.
    # The first type follows forward declarations without a blank line;
    # subsequent types are separated by the trailing traits of the
    # preceding type which already provide a blank line.
    enum_map = {e.name: e for e in schema.enums}
    unified_levels = topological_sort_types(schema, include_enums=True)
    first_type = True
    for level in unified_levels:
        for type_name in level:
            if type_name in enum_map:
                if not first_type:
                    w.line()
                w.line(f"namespace {pkg} {{")
                _emit_enum_decl(w, enum_map[type_name], pkg)
            else:
                t = schema.type_by_name(type_name)
                if not isinstance(t, ComplexType):
                    continue
                if not first_type:
                    w.line()
                w.line(f"namespace {pkg} {{")
                if t.kind == TypeKind.SEQUENCE:
                    _emit_sequence_decl(w, t, schema, pkg)
                elif t.kind == TypeKind.CHOICE:
                    _emit_choice_decl(w, t, schema, pkg)
            first_type = False

    # Inline section
    w.line()
    w.line(
        "// ============================================================================"
    )
    w.line("//                          INLINE DEFINITIONS")
    w.line(
        "// ============================================================================"
    )
    w.line()
    w.line(f"namespace {pkg} {{")

    # Inline methods — enums interleaved with classes in topo order
    for level in unified_levels:
        for type_name in level:
            if type_name in enum_map:
                _emit_enum_inline(w, enum_map[type_name])
            else:
                t = schema.type_by_name(type_name)
                if not isinstance(t, ComplexType):
                    continue
                if t.kind == TypeKind.SEQUENCE:
                    _emit_sequence_inline(w, t, schema)
                elif t.kind == TypeKind.CHOICE:
                    _emit_choice_inline(w, t, schema)

    w.line("}  // close package namespace")
    w.line()
    w.line("// FREE FUNCTIONS")
    w.line()
    w.line("}  // close enterprise namespace")
    w.line("#endif")
    w.line()
    xsd_file = xsd_name or f"{pkg}.xsd"
    ver = codegen_version or "2025.11.13"
    flags_suffix = f" {extra_flags}" if extra_flags else ""
    w.line(f"// GENERATED BY BLP_BAS_CODEGEN_{ver}")
    w.line("// USING bas_codegen.pl -m msg --noAggregateConversion --noExternalization")
    w.line(
        f"// --noIdent --package {pkg} --msgComponent {component} {xsd_file}{flags_suffix}"
    )

    return w.result()


def _emit_enum_inline(w: Writer, enum: EnumType) -> None:
    """Emit inline methods for an enum."""
    name = to_class_name(enum.name)
    w.line()
    w.separator(name)
    w.line()
    w.line("// CLASS METHODS")
    w.wrapped(
        f"inline int {name}::fromString(Value* result, const bsl::string& string)"
    )
    w.line("{")
    w.wrapped(
        "    return fromString(result, string.c_str(), static_cast<int>(string.length()));"
    )
    w.line("}")
    w.line()
    w.wrapped(
        f"inline bsl::ostream& {name}::print(bsl::ostream& stream, {name}::Value value)"
    )
    w.line("{")
    w.line("    return stream << toString(value);")
    w.line("}")


def _emit_sequence_inline(w: Writer, t: ComplexType, schema: Schema) -> None:
    """Emit inline template and accessor methods for a sequence type."""
    name = to_class_name(t.name)
    resolved = [resolve_field(f, t, schema) for f in t.fields]
    n = len(t.fields)
    use_hash_helper = n >= 3
    use_eq_helper = n >= 4

    w.line()
    w.separator(name)
    w.line()

    # PRIVATE ACCESSORS
    if use_hash_helper:
        w.line("// PRIVATE ACCESSORS")
        w.line("template <typename t_HASH_ALGORITHM>")
        _emit_paren_wrap(
            w, f"void {name}::hashAppendImpl(t_HASH_ALGORITHM& hashAlgorithm) const"
        )
        w.line("{")
        w.line("    using bslh::hashAppend;")
        for rf in resolved:
            w.line(f"    hashAppend(hashAlgorithm, this->{rf.name}());")
        w.line("}")
        w.line()

    if use_eq_helper:
        w.wrapped(f"inline bool {name}::isEqualTo(const {name}& rhs) const")
        w.line("{")
        parts = [f"this->{rf.name}() == rhs.{rf.name}()" for rf in resolved]
        _emit_comparison_chain(
            w, parts, prefix="    return ", cont_indent="           ", suffix=";"
        )
        w.line("}")
        w.line()

    # CLASS METHODS
    w.line("// CLASS METHODS")

    # MANIPULATORS
    w.line("// MANIPULATORS")
    w.line("template <typename t_MANIPULATOR>")
    _emit_paren_wrap(w, f"int {name}::manipulateAttributes(t_MANIPULATOR& manipulator)")
    w.line("{")
    if n == 0:
        w.line("    (void)manipulator;")
        w.line("    return 0;")
    else:
        w.line("    int ret;")
        w.line()
        for rf in resolved:
            m = to_member_name(rf.field.name)
            idx = f"ATTRIBUTE_INDEX_{to_upper_snake(rf.field.name)}"
            is_ptr = _needs_pointer_storage(rf.field, schema)
            if is_ptr:
                w.line(f"    BSLS_ASSERT({m});")
                w.wrapped(f"    ret = manipulator({m}, ATTRIBUTE_INFO_ARRAY[{idx}]);")
            else:
                w.wrapped(f"    ret = manipulator(&{m}, ATTRIBUTE_INFO_ARRAY[{idx}]);")
            w.line("    if (ret) {")
            w.line("        return ret;")
            w.line("    }")
            w.line()
        w.line("    return 0;")
    w.line("}")
    w.line()

    # manipulateAttribute(id)
    w.line("template <typename t_MANIPULATOR>")
    w.wrapped(f"int {name}::manipulateAttribute(t_MANIPULATOR& manipulator, int id)")
    w.line("{")
    if n == 0:
        w.line("    (void)manipulator;")
    w.line("    enum { NOT_FOUND = -1 };")
    w.line()
    w.line("    switch (id) {")
    for rf in resolved:
        m = to_member_name(rf.field.name)
        attr_id = f"ATTRIBUTE_ID_{to_upper_snake(rf.field.name)}"
        idx = f"ATTRIBUTE_INDEX_{to_upper_snake(rf.field.name)}"
        is_ptr = _needs_pointer_storage(rf.field, schema)
        w.line(f"    case {attr_id}: {{")
        if is_ptr:
            w.line(f"        BSLS_ASSERT({m});")
            w.wrapped(f"        return manipulator({m}, ATTRIBUTE_INFO_ARRAY[{idx}]);")
        else:
            w.wrapped(f"        return manipulator(&{m}, ATTRIBUTE_INFO_ARRAY[{idx}]);")
        w.line("    }")
    w.line("    default: return NOT_FOUND;")
    w.line("    }")
    w.line("}")
    w.line()

    # manipulateAttribute(name, len)
    w.line("template <typename t_MANIPULATOR>")
    w.wrapped(
        f"int {name}::manipulateAttribute(t_MANIPULATOR& manipulator, const char* name, int nameLength)"
    )
    w.line("{")
    w.line("    enum { NOT_FOUND = -1 };")
    w.line()
    w.wrapped(
        "    const bdlat_AttributeInfo* attributeInfo = lookupAttributeInfo(name, nameLength);"
    )
    w.line("    if (0 == attributeInfo) {")
    w.line("        return NOT_FOUND;")
    w.line("    }")
    w.line()
    w.line("    return manipulateAttribute(manipulator, attributeInfo->d_id);")
    w.line("}")
    w.line()

    # Mutable accessors
    for rf in resolved:
        mut_type, _ = _accessor_return(rf)
        m = to_member_name(rf.field.name)
        is_ptr = _needs_pointer_storage(rf.field, schema)
        w.wrapped(f"inline {mut_type} {name}::{rf.name}()")
        w.line("{")
        if is_ptr:
            w.line(f"    BSLS_ASSERT({m});")
            w.line(f"    return *{m};")
        else:
            w.line(f"    return {m};")
        w.line("}")
        w.line()

    # ACCESSORS
    w.line("// ACCESSORS")
    w.line("template <typename t_ACCESSOR>")
    w.wrapped(f"int {name}::accessAttributes(t_ACCESSOR& accessor) const")
    w.line("{")
    if n == 0:
        w.line("    (void)accessor;")
        w.line("    return 0;")
    else:
        w.line("    int ret;")
        w.line()
        for rf in resolved:
            m = to_member_name(rf.field.name)
            idx = f"ATTRIBUTE_INDEX_{to_upper_snake(rf.field.name)}"
            is_ptr = _needs_pointer_storage(rf.field, schema)
            if is_ptr:
                w.line(f"    BSLS_ASSERT({m});")
                w.wrapped(f"    ret = accessor(*{m}, ATTRIBUTE_INFO_ARRAY[{idx}]);")
            else:
                w.wrapped(f"    ret = accessor({m}, ATTRIBUTE_INFO_ARRAY[{idx}]);")
            w.line("    if (ret) {")
            w.line("        return ret;")
            w.line("    }")
            w.line()
        w.line("    return 0;")
    w.line("}")
    w.line()

    # accessAttribute(id)
    w.line("template <typename t_ACCESSOR>")
    w.wrapped(f"int {name}::accessAttribute(t_ACCESSOR& accessor, int id) const")
    w.line("{")
    if n == 0:
        w.line("    (void)accessor;")
    w.line("    enum { NOT_FOUND = -1 };")
    w.line()
    w.line("    switch (id) {")
    for rf in resolved:
        m = to_member_name(rf.field.name)
        attr_id = f"ATTRIBUTE_ID_{to_upper_snake(rf.field.name)}"
        idx = f"ATTRIBUTE_INDEX_{to_upper_snake(rf.field.name)}"
        is_ptr = _needs_pointer_storage(rf.field, schema)
        w.line(f"    case {attr_id}: {{")
        if is_ptr:
            w.line(f"        BSLS_ASSERT({m});")
            w.wrapped(f"        return accessor(*{m}, ATTRIBUTE_INFO_ARRAY[{idx}]);")
        else:
            w.wrapped(f"        return accessor({m}, ATTRIBUTE_INFO_ARRAY[{idx}]);")
        w.line("    }")
    w.line("    default: return NOT_FOUND;")
    w.line("    }")
    w.line("}")
    w.line()

    # accessAttribute(name, len)
    w.line("template <typename t_ACCESSOR>")
    w.wrapped(
        f"int {name}::accessAttribute(t_ACCESSOR& accessor, const char* name, int nameLength) const"
    )
    w.line("{")
    w.line("    enum { NOT_FOUND = -1 };")
    w.line()
    w.wrapped(
        "    const bdlat_AttributeInfo* attributeInfo = lookupAttributeInfo(name, nameLength);"
    )
    w.line("    if (0 == attributeInfo) {")
    w.line("        return NOT_FOUND;")
    w.line("    }")
    w.line()
    w.line("    return accessAttribute(accessor, attributeInfo->d_id);")
    w.line("}")
    if resolved:
        w.line()

    # Const accessors
    for i, rf in enumerate(resolved):
        _, const_type = _accessor_return(rf)
        m = to_member_name(rf.field.name)
        is_ptr = _needs_pointer_storage(rf.field, schema)
        w.wrapped(f"inline {const_type} {name}::{rf.name}() const")
        w.line("{")
        if is_ptr:
            w.line(f"    BSLS_ASSERT({m});")
            w.line(f"    return *{m};")
        else:
            w.line(f"    return {m};")
        w.line("}")
        if i < len(resolved) - 1:
            w.line()


def _emit_choice_inline(w: Writer, t: ComplexType, schema: Schema) -> None:
    """Emit inline template and accessor methods for a choice type."""
    name = to_class_name(t.name)
    has_alloc = _choice_needs_allocator(t, schema)
    resolved = [resolve_field(f, t, schema) for f in t.choices]

    w.line()
    w.separator(name)
    w.line()

    # PRIVATE ACCESSORS (always for choices)
    w.line("// CLASS METHODS")
    w.line("// PRIVATE ACCESSORS")
    w.line("template <typename t_HASH_ALGORITHM>")
    _emit_paren_wrap(
        w, f"void {name}::hashAppendImpl(t_HASH_ALGORITHM& hashAlgorithm) const"
    )
    w.line("{")
    w.line(f"    typedef {name} Class;")
    w.line("    using bslh::hashAppend;")
    w.line("    hashAppend(hashAlgorithm, this->selectionId());")
    w.line("    switch (this->selectionId()) {")
    for rf in resolved:
        sel_id = f"Class::SELECTION_ID_{to_upper_snake(rf.field.name)}"
        w.line(f"    case {sel_id}:")
        w.line(f"        hashAppend(hashAlgorithm, this->{rf.name}());")
        w.line("        break;")
    w.line(
        "    default: BSLS_ASSERT(this->selectionId() == Class::SELECTION_ID_UNDEFINED);"
    )
    w.line("    }")
    w.line("}")
    w.line()

    # isEqualTo (always for choices)
    w.wrapped(f"inline bool {name}::isEqualTo(const {name}& rhs) const")
    w.line("{")
    w.line(f"    typedef {name} Class;")
    w.line("    if (this->selectionId() == rhs.selectionId()) {")
    w.line("        switch (rhs.selectionId()) {")
    for rf in resolved:
        sel_id = f"Class::SELECTION_ID_{to_upper_snake(rf.field.name)}"
        # Try single-line: case X: return this->x() == rhs.x();
        single = f"        case {sel_id}: return this->{rf.name}() == rhs.{rf.name}();"
        if len(single) <= 79:
            w.line(single)
        else:
            w.line(f"        case {sel_id}:")
            ret_line = f"            return this->{rf.name}() == rhs.{rf.name}();"
            if len(ret_line) <= 79:
                w.line(ret_line)
            else:
                w.line(f"            return this->{rf.name}() ==")
                w.line(f"                   rhs.{rf.name}();")
    w.line("        default:")
    w.line(
        "            BSLS_ASSERT(Class::SELECTION_ID_UNDEFINED == rhs.selectionId());"
    )
    w.line("            return true;")
    w.line("        }")
    w.line("    }")
    w.line("    else {")
    w.line("        return false;")
    w.line("    }")
    w.line("}")
    w.line()

    # CREATORS (default ctor and destructor are inline for choices)
    w.line("// CREATORS")
    if has_alloc:
        _emit_paren_wrap(w, f"inline {name}::{name}(bslma::Allocator* basicAllocator)")
        w.line(": d_selectionId(SELECTION_ID_UNDEFINED)")
        w.line(", d_allocator_p(bslma::Default::allocator(basicAllocator))")
    else:
        w.wrapped(f"inline {name}::{name}()")
        w.line(": d_selectionId(SELECTION_ID_UNDEFINED)")
    w.line("{")
    w.line("}")
    w.line()

    w.wrapped(f"inline {name}::~{name}()")
    w.line("{")
    w.line("    reset();")
    w.line("}")
    w.line()

    # MANIPULATORS - manipulateSelection (template)
    w.line("// MANIPULATORS")
    w.line("template <typename t_MANIPULATOR>")
    w.wrapped(f"int {name}::manipulateSelection(t_MANIPULATOR& manipulator)")
    w.line("{")
    w.line("    switch (d_selectionId) {")
    for rf in resolved:
        m = to_member_name(rf.field.name)
        sel_id = f"{name}::SELECTION_ID_{to_upper_snake(rf.field.name)}"
        idx = f"SELECTION_INDEX_{to_upper_snake(rf.field.name)}"
        w.line(f"    case {sel_id}:")
        w.wrapped(
            f"        return manipulator(&{m}.object(), SELECTION_INFO_ARRAY[{idx}]);"
        )
    w.line("    default:")
    _emit_bsls_assert(
        w, f"        BSLS_ASSERT({name}::SELECTION_ID_UNDEFINED == d_selectionId);"
    )
    w.line("        return -1;")
    w.line("    }")
    w.line("}")
    w.line()

    # Mutable accessors
    for rf in resolved:
        m = to_member_name(rf.field.name)
        sel_id = f"SELECTION_ID_{to_upper_snake(rf.field.name)}"
        w.wrapped(f"inline {rf.cpp_type}& {name}::{rf.name}()")
        w.line("{")
        w.line(f"    BSLS_ASSERT({sel_id} == d_selectionId);")
        w.line(f"    return {m}.object();")
        w.line("}")
        w.line()

    # ACCESSORS
    w.line("// ACCESSORS")
    w.wrapped(f"inline int {name}::selectionId() const")
    w.line("{")
    w.line("    return d_selectionId;")
    w.line("}")
    w.line()

    # accessSelection (template)
    w.line("template <typename t_ACCESSOR>")
    w.wrapped(f"int {name}::accessSelection(t_ACCESSOR& accessor) const")
    w.line("{")
    w.line("    switch (d_selectionId) {")
    for rf in resolved:
        m = to_member_name(rf.field.name)
        sel_id = f"SELECTION_ID_{to_upper_snake(rf.field.name)}"
        idx = f"SELECTION_INDEX_{to_upper_snake(rf.field.name)}"
        w.line(f"    case {sel_id}:")
        w.wrapped(
            f"        return accessor({m}.object(), SELECTION_INFO_ARRAY[{idx}]);"
        )
    w.line(
        "    default: BSLS_ASSERT(SELECTION_ID_UNDEFINED == d_selectionId); return -1;"
    )
    w.line("    }")
    w.line("}")
    w.line()

    # Const accessors
    for rf in resolved:
        m = to_member_name(rf.field.name)
        sel_id = f"SELECTION_ID_{to_upper_snake(rf.field.name)}"
        w.wrapped(f"inline const {rf.cpp_type}& {name}::{rf.name}() const")
        w.line("{")
        w.line(f"    BSLS_ASSERT({sel_id} == d_selectionId);")
        w.line(f"    return {m}.object();")
        w.line("}")
        w.line()

    # is*Value() methods
    for rf in resolved:
        sel_id = f"SELECTION_ID_{to_upper_snake(rf.field.name)}"
        w.wrapped(f"inline bool {name}::is{_title_case(rf.field.name)}Value() const")
        w.line("{")
        w.line(f"    return {sel_id} == d_selectionId;")
        w.line("}")
        w.line()

    w.wrapped(f"inline bool {name}::isUndefinedValue() const")
    w.line("{")
    w.line("    return SELECTION_ID_UNDEFINED == d_selectionId;")
    w.line("}")
