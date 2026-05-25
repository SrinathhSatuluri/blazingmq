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

"""Emit the .cpp file for a BDE-style generated component."""

from __future__ import annotations

from .emitter_h import _needs_pointer_storage
from .model import ComplexType, EnumType, Field, FieldKind, Schema, TypeKind, XSD_TYPE_MAP
from .naming import (
    make_banner,
    to_accessor_name,
    to_class_name,
    to_enum_value,
    to_member_name,
    to_title_name,
    to_upper_snake,
)
from .ordering import sort_fields_by_alignment, topological_sort_types
from .resolver import ResolvedField, resolve_field, type_needs_allocator
from .hybrid import expand_hybrids
from .writer import Writer

def _build_includes_cpp(schema: Schema) -> str:
    """Build the cpp include block, only emitting headers for types used."""
    used_xsd_types: set[str] = set()
    has_nullable_allocated = False
    has_nullable = False
    for t in schema.types:
        if isinstance(t, ComplexType):
            for f in t.fields:
                used_xsd_types.add(f.type_name)
                if f.kind == FieldKind.OPTIONAL:
                    ref_name = f.type_name.removeprefix("tns:")
                    if ref_name == t.name:
                        has_nullable_allocated = True
                    else:
                        has_nullable = True

    has_any_datetime = bool({"xs:date", "xs:time", "xs:dateTime"} & used_xsd_types)
    needs_date = has_any_datetime
    needs_time = has_any_datetime
    needs_datetime = has_any_datetime

    lines = [
        "#include <bdlat_formattingmode.h>",
        "#include <bdlat_valuetypefunctions.h>",
        "#include <bdlb_print.h>",
        "#include <bdlb_printmethods.h>",
        "#include <bdlb_string.h>",
        "",
    ]

    conditional = []
    if has_nullable_allocated:
        conditional.append("#include <bdlb_nullableallocatedvalue.h>")
    if has_nullable or has_nullable_allocated:
        conditional.append("#include <bdlb_nullablevalue.h>")
    if needs_datetime:
        conditional.append("#include <bdlt_datetimetz.h>")
    if needs_date:
        conditional.append("#include <bdlt_datetz.h>")
    if needs_time:
        conditional.append("#include <bdlt_timetz.h>")

    lines += conditional
    lines += [
        "#include <bsl_string.h>",
        "#include <bsl_vector.h>",
        "#include <bslim_printer.h>",
        "#include <bsls_assert.h>",
        "#include <bsls_types.h>",
        "",
        "#include <bsl_cstring.h>",
        "#include <bsl_iomanip.h>",
        "#include <bsl_limits.h>",
        "#include <bsl_ostream.h>",
        "#include <bsl_utility.h>",
    ]

    return "\n".join(lines)


# Fundamental C++ types are passed by value (not const-ref) in BDE codegen.
_FUNDAMENTAL_CPP_TYPES = frozenset(
    {
        "bool",
        "char",
        "short",
        "int",
        "unsigned int",
        "long",
        "unsigned long",
        "float",
        "double",
        "bsls::Types::Int64",
        "bsls::Types::Uint64",
    }
)


# ---------------------------------------------------------------------------
# Enum implementation
# ---------------------------------------------------------------------------


def _emit_enum_impl(w: Writer, enum: EnumType) -> None:
    """Emit .cpp implementation for an enum type."""
    name = to_class_name(enum.name)
    n = len(enum.values)

    w.line()
    w.separator(name)
    w.line()
    w.line("// CONSTANTS")
    w.line()
    class_name_line = f'const char {name}::CLASS_NAME[] = "{name}";'
    if len(class_name_line) <= 79:
        w.line(class_name_line)
    else:
        w.line(f"const char {name}::CLASS_NAME[] =")
        w.line(f'    "{name}";')
    w.line()
    enum_decl = f"const bdlat_EnumeratorInfo {name}::ENUMERATOR_INFO_ARRAY[] ="
    decl_split = len(enum_decl + " {") > 79
    if not decl_split:
        w.line(enum_decl + " {")
    else:
        w.line(enum_decl)
    for i, v in enumerate(enum.values):
        is_last = i == n - 1
        ev = f"e_{to_enum_value(v.name)}"
        close = "}};" if is_last else "},"
        # When declaration splits: first uses "    {{", rest use "     {"
        if decl_split:
            brace_prefix = "    {{" if i == 0 else "     {"
            field_indent = "      "
        else:
            brace_prefix = "    {"
            field_indent = "     "
        single = (
            f'{brace_prefix}{name}::{ev}, "{v.name}", sizeof("{v.name}") - 1, ""{close}'
        )
        if len(single) <= 79:
            w.line(single)
        else:
            w.line(f"{brace_prefix}{name}::{ev},")
            w.line(f'{field_indent}"{v.name}",')
            w.line(f'{field_indent}sizeof("{v.name}") - 1,')
            w.line(f'{field_indent}""{close}')
    w.line()
    w.line("// CLASS METHODS")
    w.line()

    # fromInt — use w.wrapped() unless paren-aligned wrapping fails
    # (i.e. the first param line exceeds 79), in which case use
    # fallback wrapping to avoid return-type splitting for 'int'.
    from_int_line = f"int {name}::fromInt({name}::Value* result, int number)"
    from_int_prefix = f"int {name}::fromInt("
    from_int_first = f"{from_int_prefix}{name}::Value* result,"
    if len(from_int_line) <= 79:
        w.line(from_int_line)
    elif len(from_int_first) <= 79:
        w.wrapped(from_int_line)
    else:
        type_w = len(f"{name}::Value*")
        w.line(from_int_prefix)
        w.line(f"    {name}::Value* result,")
        w.line(f"    {'int'.ljust(type_w)} number)")
    w.line("{")
    w.line("    switch (number) {")
    for v in enum.values:
        w.line(f"    case {name}::e_{to_enum_value(v.name)}:")
    w.line(f"        *result = static_cast<{name}::Value>(number);")
    w.line("        return 0;")
    w.line("    default: return -1;")
    w.line("    }")
    w.line("}")
    w.line()

    # fromString — same approach
    from_str_line = f"int {name}::fromString({name}::Value* result, const char* string, int stringLength)"
    from_str_prefix = f"int {name}::fromString("
    from_str_first = f"{from_str_prefix}{name}::Value* result,"
    if len(from_str_line) <= 79:
        w.line(from_str_line)
    elif len(from_str_first) <= 79:
        w.wrapped(from_str_line)
    else:
        type_w = len(f"{name}::Value*")
        w.line(from_str_prefix)
        w.line(f"    {name}::Value* result,")
        w.line(f"    {'const char*'.ljust(type_w)} string,")
        w.line(f"    {'int'.ljust(type_w)} stringLength)")
    w.line("{")
    w.line(f"    for (int i = 0; i < {n}; ++i) {{")
    w.line("        const bdlat_EnumeratorInfo& enumeratorInfo =")
    w.line(f"            {name}::ENUMERATOR_INFO_ARRAY[i];")
    w.line()
    w.line("        if (stringLength == enumeratorInfo.d_nameLength &&")
    w.line(
        "            0 == bsl::memcmp(enumeratorInfo.d_name_p, string, stringLength)) {"
    )
    cast_line = (
        f"            *result = static_cast<{name}::Value>(enumeratorInfo.d_value);"
    )
    if len(cast_line) <= 79:
        w.line(cast_line)
    else:
        w.line(f"            *result = static_cast<{name}::Value>(")
        w.line("                enumeratorInfo.d_value);")
    w.line("            return 0;")
    w.line("        }")
    w.line("    }")
    w.line()
    w.line("    return -1;")
    w.line("}")
    w.line()

    # toString
    w.wrapped(f"const char* {name}::toString({name}::Value value)")
    w.line("{")
    w.line("    switch (value) {")
    for v in enum.values:
        w.line(f"    case e_{to_enum_value(v.name)}: {{")
        w.line(f'        return "{v.name}";')
        w.line("    }")
    w.line("    }")
    w.line()
    w.line('    BSLS_ASSERT(!"invalid enumerator");')
    w.line("    return 0;")
    w.line("}")


# ---------------------------------------------------------------------------
# Sequence implementation
# ---------------------------------------------------------------------------


def _default_value_literal(rf: ResolvedField, f: Field) -> str:
    """Return the C++ literal for a field's default value."""
    val = f.default
    if rf.field.type_name == "xs:boolean":
        return "true" if val == "true" else "false"
    if rf.field.type_name in ("xs:int", "xs:unsignedInt", "xs:long", "xs:unsignedLong",
                               "xs:integer", "xs:nonNegativeInteger"):
        return val
    if rf.field.type_name in ("xs:double", "xs:decimal"):
        return val
    if rf.field.type_name == "xs:string":
        return f'"{val}"'
    # Enum type — the default is the enumerator name (e.g. "TEXT" -> EncodingFormat::TEXT)
    ref_name = rf.field.type_name.removeprefix("tns:")
    return f"{to_class_name(ref_name)}::{to_enum_value(val)}"


def _formatting_mode_expr(
    rf: ResolvedField, f: Field, is_hybrid_choice: bool = False
) -> str:
    """Build the bdlat_FormattingMode expression for ATTRIBUTE_INFO_ARRAY."""
    mode = f"bdlat_FormattingMode::{rf.formatting_mode}"
    if is_hybrid_choice:
        mode += " | bdlat_FormattingMode::e_UNTAGGED"
    if f.default is not None:
        mode += " | bdlat_FormattingMode::e_DEFAULT_VALUE"
    return mode


def _emit_sequence_impl(
    w: Writer, t: ComplexType, schema: Schema,
    hybrid_choice_names: dict[str, list[str]] | None = None,
) -> None:
    """Emit .cpp implementation for a sequence type."""
    name = to_class_name(t.name)
    has_alloc = type_needs_allocator(t, schema)
    resolved = [resolve_field(f, t, schema) for f in t.fields]
    sorted_fields = sort_fields_by_alignment(t.fields, schema, t)
    sorted_resolved = [resolve_field(f, t, schema) for f in sorted_fields]
    n = len(t.fields)
    hybrid_selections = (hybrid_choice_names or {}).get(t.name)

    ptr_fields = [f for f in t.fields if _needs_pointer_storage(f, schema)]
    ptr_members = {to_member_name(f.name) for f in ptr_fields}
    has_ptr = len(ptr_fields) > 0
    sorted_resolved_no_ptr = [
        rf for rf in sorted_resolved if to_member_name(rf.field.name) not in ptr_members
    ]

    w.line()
    w.separator(name)
    w.line()
    w.line("// CONSTANTS")
    w.line()
    class_name_line = f'const char {name}::CLASS_NAME[] = "{name}";'
    if len(class_name_line) <= 79:
        w.line(class_name_line)
    else:
        w.line(f"const char {name}::CLASS_NAME[] =")
        w.line(f'    "{name}";')

    # DEFAULT_INITIALIZER constants for fields with defaults
    for rf, f in zip(resolved, t.fields):
        if f.default is not None:
            const_name = f"DEFAULT_INITIALIZER_{to_upper_snake(f.name)}"
            w.line()
            if rf.field.type_name == "xs:string":
                w.line(f"const char {name}::{const_name}[] = {_default_value_literal(rf, f)};")
            else:
                init_line = f"const {rf.cpp_type} {name}::{const_name} = {_default_value_literal(rf, f)};"
                if len(init_line) <= 79:
                    w.line(init_line)
                else:
                    # Try breaking after '=' first (for short types like int)
                    eq_break = f"const {rf.cpp_type} {name}::{const_name} ="
                    if len(eq_break) <= 79:
                        w.line(eq_break)
                        w.line(f"    {_default_value_literal(rf, f)};")
                    else:
                        # Long type: break after type name
                        w.line(f"const {rf.cpp_type}")
                        w.line(f"    {name}::{const_name} = {_default_value_literal(rf, f)};")

    if n > 0:
        w.line()
        array_decl = f"const bdlat_AttributeInfo {name}::ATTRIBUTE_INFO_ARRAY[] ="
        attr_decl_split = len(array_decl + " {") > 79
        if not attr_decl_split:
            w.line(array_decl + " {")
            brace_prefix_first = "    {"
            brace_prefix_rest = "    {"
            field_indent = "     "
        else:
            w.line("const bdlat_AttributeInfo")
            w.line(f"    {name}::ATTRIBUTE_INFO_ARRAY[] = {{")
            brace_prefix_first = "        {"
            brace_prefix_rest = "        {"
            field_indent = "         "
        for i, (rf, f) in enumerate(zip(resolved, t.fields)):
            is_last = i == n - 1
            attr_id = f"ATTRIBUTE_ID_{to_upper_snake(f.name)}"
            is_hybrid_choice = f.name == "choice" and hybrid_selections is not None
            mode = _formatting_mode_expr(rf, f, is_hybrid_choice)
            field_name = "Choice" if is_hybrid_choice else f.name
            close = "}};" if is_last else "},"
            brace_prefix = brace_prefix_first if i == 0 else brace_prefix_rest
            single = f'{brace_prefix}{attr_id}, "{field_name}", sizeof("{field_name}") - 1, "", {mode}{close}'
            if len(single) <= 79:
                w.line(single)
            else:
                w.line(f"{brace_prefix}{attr_id},")
                w.line(f'{field_indent}"{field_name}",')
                w.line(f'{field_indent}sizeof("{field_name}") - 1,')
                w.line(f'{field_indent}"",')
                if is_last:
                    w.line(f"{field_indent}{mode}}}}};")
                else:
                    w.line(f"{field_indent}{mode}}},")

    w.line()
    w.line("// CLASS METHODS")
    w.line()

    # lookupAttributeInfo(name, len)
    w.wrapped(
        f"const bdlat_AttributeInfo* {name}::lookupAttributeInfo(const char* name, int nameLength)"
    )
    w.line("{")
    if hybrid_selections:
        # For hybrid types, choice selection names map to the CHOICE attribute
        for sel_name in hybrid_selections:
            if_line = f'    if (bdlb::String::areEqualCaseless("{sel_name}", name, nameLength)) {{'
            if len(if_line) <= 79:
                w.line(if_line)
            else:
                align = len("    if (bdlb::String::areEqualCaseless(")
                w.line(f'    if (bdlb::String::areEqualCaseless("{sel_name}",')
                w.line(f"{' ' * align}name,")
                w.line(f"{' ' * align}nameLength)) {{")
            w.line("        return &ATTRIBUTE_INFO_ARRAY[ATTRIBUTE_INDEX_CHOICE];")
            w.line("    }")
            w.line()
    if n > 0:
        w.line(f"    for (int i = 0; i < {n}; ++i) {{")
        attr_assign = f"        const bdlat_AttributeInfo& attributeInfo = {name}::ATTRIBUTE_INFO_ARRAY[i];"
        if len(attr_assign) > 79:
            w.line("        const bdlat_AttributeInfo& attributeInfo =")
            w.line(f"            {name}::ATTRIBUTE_INFO_ARRAY[i];")
        else:
            w.line(attr_assign)
        w.line()
        w.line("        if (nameLength == attributeInfo.d_nameLength &&")
        w.line(
            "            0 == bsl::memcmp(attributeInfo.d_name_p, name, nameLength)) {"
        )
        w.line("            return &attributeInfo;")
        w.line("        }")
        w.line("    }")
        w.line()
    else:
        # Empty types: suppress unused-parameter warnings
        w.line("    (void)name;")
        w.line("    (void)nameLength;")
    w.line("    return 0;")
    w.line("}")
    w.line()

    # lookupAttributeInfo(id)
    w.wrapped(f"const bdlat_AttributeInfo* {name}::lookupAttributeInfo(int id)")
    w.line("{")
    if n > 0:
        w.line("    switch (id) {")
        for f in t.fields:
            id_name = f"ATTRIBUTE_ID_{to_upper_snake(f.name)}"
            idx_name = f"ATTRIBUTE_INDEX_{to_upper_snake(f.name)}"
            single = f"    case {id_name}: return &ATTRIBUTE_INFO_ARRAY[{idx_name}];"
            if len(single) <= 79:
                w.line(single)
            else:
                w.line(f"    case {id_name}:")
                ret_line = f"        return &ATTRIBUTE_INFO_ARRAY[{idx_name}];"
                if len(ret_line) <= 79:
                    w.line(ret_line)
                else:
                    w.line("        return &ATTRIBUTE_INFO_ARRAY")
                    w.line(f"            [{idx_name}];")
        w.line("    default: return 0;")
    else:
        w.line("    switch (id) {")
        w.line("    default: return 0;")
    w.line("    }")
    w.line("}")

    # CREATORS
    w.line()
    w.line("// CREATORS")
    if has_alloc or n > 0:
        w.line()
    if has_alloc:
        init_resolved = sorted_resolved_no_ptr if has_ptr else sorted_resolved

        # Default constructor with allocator
        w.wrapped(f"{name}::{name}(bslma::Allocator* basicAllocator)")
        if has_ptr:
            w.line(": d_allocator_p(bslma::Default::allocator(basicAllocator))")
            _emit_initializer_list(w, init_resolved, has_alloc, is_copy=False, first=False)
        else:
            _emit_initializer_list(w, init_resolved, has_alloc, is_copy=False)
        w.line("{")
        for pf in ptr_fields:
            pm = to_member_name(pf.name)
            pt = to_class_name(pf.type_name.removeprefix("tns:"))
            w.line(f"    {pm} = new (*d_allocator_p) {pt}(d_allocator_p);")
        w.line("}")
        w.line()

        # Copy constructor
        w.wrapped(
            f"{name}::{name}(const {name}& original, bslma::Allocator* basicAllocator)"
        )
        if has_ptr:
            w.line(": d_allocator_p(bslma::Default::allocator(basicAllocator))")
            _emit_initializer_list(w, init_resolved, has_alloc, is_copy=True, first=False)
        else:
            _emit_initializer_list(w, init_resolved, has_alloc, is_copy=True)
        w.line("{")
        for pf in ptr_fields:
            pm = to_member_name(pf.name)
            pt = to_class_name(pf.type_name.removeprefix("tns:"))
            new_expr = f"    {pm} = new (*d_allocator_p)"
            full = f"{new_expr} {pt}(*original.{pm}, d_allocator_p);"
            if len(full) <= 79:
                w.line(full)
            else:
                w.line(new_expr)
                w.line(f"        {pt}(*original.{pm}, d_allocator_p);")
        w.line("}")
        w.line()

        # Move constructors (ifdef'd)
        w.line(
            "#if defined(BSLS_COMPILERFEATURES_SUPPORT_RVALUE_REFERENCES) &&               \\"
        )
        w.line("    defined(BSLS_COMPILERFEATURES_SUPPORT_NOEXCEPT)")
        if has_ptr:
            _emit_noexcept_move_ctor(w, name, init_resolved, alloc_prefix="d_allocator_p(original.d_allocator_p)")
        else:
            _emit_noexcept_move_ctor(w, name, init_resolved)
        w.line("{")
        for pf in ptr_fields:
            pm = to_member_name(pf.name)
            max_w = max(len(pm), len(f"original.{pm}"))
            w.line(f"    {pm.ljust(max_w)} = original.{pm};")
            w.line(f"    {'original.' + pm:<{max_w}} = 0;")
        w.line("}")
        w.line()
        w.wrapped(
            f"{name}::{name}({name}&& original, bslma::Allocator* basicAllocator)"
        )
        if has_ptr:
            w.line(": d_allocator_p(bslma::Default::allocator(basicAllocator))")
            _emit_move_init_list(w, init_resolved, with_allocator=True, first=False)
        else:
            _emit_move_init_list(w, init_resolved, with_allocator=True)
        w.line("{")
        for pf in ptr_fields:
            pm = to_member_name(pf.name)
            pt = to_class_name(pf.type_name.removeprefix("tns:"))
            max_w = max(len(pm), len(f"original.{pm}"))
            w.line(f"    if (d_allocator_p == original.d_allocator_p) {{")
            w.line(f"        {pm.ljust(max_w)} = original.{pm};")
            w.line(f"        {'original.' + pm:<{max_w}} = 0;")
            w.line("    }")
            w.line("    else {")
            new_expr = f"        {pm} = new (*d_allocator_p)"
            full = f"{new_expr} {pt}(bsl::move(*original.{pm}), d_allocator_p);"
            if len(full) <= 79:
                w.line(full)
            else:
                w.line(new_expr)
                w.line(f"            {pt}(bsl::move(*original.{pm}), d_allocator_p);")
            w.line("    }")
        w.line("}")
        w.line("#endif")
        w.line()

        # Destructor
        w.line(f"{name}::~{name}()")
        w.line("{")
        for pf in ptr_fields:
            pm = to_member_name(pf.name)
            w.line(f"    d_allocator_p->deleteObject({pm});")
        w.line("}")
    else:
        # Empty non-allocator types: Bloomberg omits the default constructor
        if n > 0:
            w.line(f"{name}::{name}()")
            _emit_initializer_list(w, sorted_resolved, has_alloc, is_copy=False)
            w.line("{")
            w.line("}")

    # MANIPULATORS
    w.line()
    w.line("// MANIPULATORS")
    w.line()
    if has_alloc:
        # Copy/move assignment — split members into groups at pointer boundaries
        members = [to_member_name(rf.field.name) for rf in resolved]
        member_groups: list[list[str]] = []
        cur_group: list[str] = []
        for m in members:
            if m in ptr_members:
                if cur_group:
                    member_groups.append(cur_group)
                    cur_group = []
                member_groups.append([m])
            else:
                cur_group.append(m)
        if cur_group:
            member_groups.append(cur_group)

        def _emit_assignment(move: bool) -> None:
            for group in member_groups:
                if len(group) == 1 and group[0] in ptr_members:
                    pm = group[0]
                    pf = next(f for f in ptr_fields if to_member_name(f.name) == pm)
                    pt = to_class_name(pf.type_name.removeprefix("tns:"))
                    if move:
                        max_w = max(len(pm), len(f"rhs.{pm}"))
                        w.line("        if (d_allocator_p == rhs.d_allocator_p) {")
                        w.line(f"            d_allocator_p->deleteObject({pm});")
                        w.line(f"            {pm.ljust(max_w)} = rhs.{pm};")
                        w.line(f"            {'rhs.' + pm:<{max_w}} = 0;")
                        w.line("        }")
                        w.line(f"        else if ({pm}) {{")
                        w.line(f"            *{pm} = bsl::move(*rhs.{pm});")
                        w.line("        }")
                        w.line("        else {")
                        new_expr = f"            {pm} = new (*d_allocator_p)"
                        full = f"{new_expr} {pt}(bsl::move(*rhs.{pm}), d_allocator_p);"
                        if len(full) <= 79:
                            w.line(full)
                        else:
                            w.line(new_expr)
                            w.line(f"                {pt}(bsl::move(*rhs.{pm}), d_allocator_p);")
                        w.line("        }")
                    else:
                        w.line(f"        if ({pm}) {{")
                        w.line(f"            *{pm} = *rhs.{pm};")
                        w.line("        }")
                        w.line("        else {")
                        new_expr = f"            {pm} = new (*d_allocator_p)"
                        full = f"{new_expr} {pt}(*rhs.{pm}, d_allocator_p);"
                        if len(full) <= 79:
                            w.line(full)
                        else:
                            w.line(new_expr)
                            w.line(f"                {pt}(*rhs.{pm}, d_allocator_p);")
                        w.line("        }")
                else:
                    if move:
                        maxs = _grouped_alignment(
                            group, lambda m, w: f"        {m.ljust(w)} = bsl::move(rhs.{m});",
                            include_wrap_width=True,
                        )
                    else:
                        maxs = _grouped_alignment(
                            group, lambda m, w: f"        {m.ljust(w)} = rhs.{m};"
                        )
                    for m, max_m in zip(group, maxs):
                        if move:
                            line = f"        {m.ljust(max_m)} = bsl::move(rhs.{m});"
                        else:
                            line = f"        {m.ljust(max_m)} = rhs.{m};"
                        if len(line) <= 79:
                            w.line(line)
                        elif move:
                            w.line(f"        {m} = bsl::move(")
                            w.line(f"            rhs.{m});")
                        else:
                            w.line(f"        {m} =")
                            w.line(f"            rhs.{m};")

        # Copy assignment
        w.wrapped(f"{name}& {name}::operator=(const {name}& rhs)")
        w.line("{")
        w.line("    if (this != &rhs) {")
        _emit_assignment(move=False)
        w.line("    }")
        w.line()
        w.line("    return *this;")
        w.line("}")
        w.line()

        # Move assignment
        w.line(
            "#if defined(BSLS_COMPILERFEATURES_SUPPORT_RVALUE_REFERENCES) &&               \\"
        )
        w.line("    defined(BSLS_COMPILERFEATURES_SUPPORT_NOEXCEPT)")
        w.wrapped(f"{name}& {name}::operator=({name}&& rhs)")
        w.line("{")
        w.line("    if (this != &rhs) {")
        _emit_assignment(move=True)
        w.line("    }")
        w.line()
        w.line("    return *this;")
        w.line("}")
        w.line("#endif")
        w.line()

    # reset()
    w.line(f"void {name}::reset()")
    w.line("{")
    # Compute max member name length for column alignment of '=' in assignments
    assign_members = [
        to_member_name(rf.field.name) for rf in resolved
        if rf.field.default is not None and to_member_name(rf.field.name) not in ptr_members
    ]
    max_assign_len = max((len(m) for m in assign_members), default=0) if len(assign_members) > 1 else 0
    for rf in resolved:
        m = to_member_name(rf.field.name)
        if rf.field.default is not None:
            const_name = f"DEFAULT_INITIALIZER_{to_upper_snake(rf.field.name)}"
            if max_assign_len > 0:
                padded = m.ljust(max_assign_len)
                w.line(f"    {padded} = {const_name};")
            else:
                w.line(f"    {m} = {const_name};")
        elif m in ptr_members:
            w.line(f"    BSLS_ASSERT({m});")
            reset_line = f"    bdlat_ValueTypeFunctions::reset({m});"
            if len(reset_line) <= 79:
                w.line(reset_line)
            else:
                w.line("    bdlat_ValueTypeFunctions::reset(")
                w.line(f"        {m});")
        else:
            reset_line = f"    bdlat_ValueTypeFunctions::reset(&{m});"
            if len(reset_line) <= 79:
                w.line(reset_line)
            else:
                w.line("    bdlat_ValueTypeFunctions::reset(")
                w.line(f"        &{m});")
    w.line("}")

    # ACCESSORS
    w.line()
    w.line("// ACCESSORS")
    w.line()
    if n == 0:
        # Empty types: unnamed params, no printer — just return stream
        w.wrapped(f"bsl::ostream& {name}::print(bsl::ostream& stream, int, int) const")
        w.line("{")
        w.line("    return stream;")
        w.line("}")
    else:
        w.wrapped(
            f"bsl::ostream& {name}::print(bsl::ostream& stream, int level, int spacesPerLevel) const"
        )
        w.line("{")
        w.line("    bslim::Printer printer(&stream, level, spacesPerLevel);")
        w.line("    printer.start();")
        for rf in resolved:
            acc = rf.name  # accessor name (lowercase first char)
            if rf.field.type_name == "xs:hexBinary":
                w.line("    {")
                w.line("        bool multilineFlag = (0 <= spacesPerLevel);")
                w.line("        bdlb::Print::indent(stream, level + 1, spacesPerLevel);")
                w.line('        stream << (multilineFlag ? "" : " ");')
                w.line(f'        stream << "{acc} = [ ";')
                w.line("        bdlb::Print::singleLineHexDump(stream,")
                w.line(f"                                       this->{acc}().begin(),")
                w.line(f"                                       this->{acc}().end());")
                w.line('        stream << " ]" << (multilineFlag ? "\\n" : "");')
                w.line("    }")
            else:
                pa_line = f'    printer.printAttribute("{acc}", this->{acc}());'
                if len(pa_line) <= 79:
                    w.line(pa_line)
                else:
                    w.line(f'    printer.printAttribute("{acc}",')
                    paren_col = len("    printer.printAttribute(")
                    w.line(f"{' ' * paren_col}this->{acc}());")
        w.line("    printer.end();")
        w.line("    return stream;")
        w.line("}")


def _grouped_alignment(
    members: list[str], line_fn, *, include_wrap_width: bool = False
) -> list[int]:
    """Return per-member alignment widths, grouping by wrapping boundaries.

    Members whose unpadded line exceeds 79 chars act as group boundaries.
    Within each group, all members are padded to the group's longest name.

    When *include_wrap_width* is True, a wrapping member's name width is
    included in the preceding group's max (used for move-assignment where
    Bloomberg aligns preceding members to the wrapping member's column).

    *line_fn(member, width) -> str* builds the full line for a member name
    padded to *width* characters.
    """
    n = len(members)
    result = [len(m) for m in members]

    wraps = [len(line_fn(m, len(m))) > 79 for m in members]

    i = 0
    while i < n:
        if wraps[i]:
            i += 1
            continue
        start = i
        while i < n and not wraps[i]:
            i += 1
        group_max = max(len(members[j]) for j in range(start, i))
        if include_wrap_width and i < n and wraps[i]:
            group_max = max(group_max, len(members[i]))
        for j in range(start, i):
            if len(line_fn(members[j], group_max)) <= 79:
                result[j] = group_max

    return result


def _emit_init_member(
    w: Writer, prefix: str, member: str, args: str, suffix: str = ""
) -> None:
    """Emit a single member initializer, wrapping if it exceeds 79 chars.

    Wrapping rules:
    1. If the full line fits (≤79): single line.
    2. If multi-arg and paren-aligned fits: wrap at comma, paren-align.
    3. Fallback: split at opening '(', continuation at prefix_len + 4.
    """
    line = f"{prefix}{member}({args}){suffix}"
    if len(line) <= 79:
        w.line(line)
        return

    # Multi-arg: try paren-aligned wrapping
    if ", " in args:
        arg_parts = args.split(", ")
        paren_col = len(prefix) + len(member) + 1  # column after '('
        first_line = f"{prefix}{member}({arg_parts[0]},"
        if len(first_line) <= 79:
            paren_pad = " " * paren_col
            all_fit = all(
                len(
                    f"{paren_pad}{arg_parts[i]}"
                    f"{')' if i == len(arg_parts) - 1 else ','}"
                )
                <= 79
                for i in range(1, len(arg_parts))
            )
            if all_fit:
                w.line(first_line)
                for i in range(1, len(arg_parts)):
                    sfx = ")" if i == len(arg_parts) - 1 else ","
                    w.line(f"{paren_pad}{arg_parts[i]}{sfx}")
                return

    # Fallback: split at opening paren
    fallback_indent = " " * (len(prefix) + 4)
    w.line(f"{prefix}{member}(")
    w.line(f"{fallback_indent}{args}){suffix}")


def _emit_initializer_list(
    w: Writer, sorted_resolved: list[ResolvedField], _has_alloc: bool, is_copy: bool,
    first: bool = True,
) -> None:
    """Emit the member initializer list for a constructor."""
    if not sorted_resolved:
        return

    for i, rf in enumerate(sorted_resolved):
        prefix = (": " if first else ", ") if i == 0 else ", "
        m = to_member_name(rf.field.name)
        if is_copy:
            if rf.needs_allocator:
                _emit_init_member(w, prefix, m, f"original.{m}, basicAllocator")
            else:
                w.line(f"{prefix}{m}(original.{m})")
        else:
            if rf.field.default is not None:
                default_const = f"DEFAULT_INITIALIZER_{to_upper_snake(rf.field.name)}"
                if rf.needs_allocator:
                    w.line(f"{prefix}{m}({default_const}, basicAllocator)")
                else:
                    w.line(f"{prefix}{m}({default_const})")
            elif rf.needs_allocator:
                w.line(f"{prefix}{m}(basicAllocator)")
            elif rf.is_enum:
                w.line(f"{prefix}{m}(static_cast<{rf.cpp_type}>(0))")
            else:
                w.line(f"{prefix}{m}()")


def _emit_move_init_list(
    w: Writer, sorted_resolved: list[ResolvedField], with_allocator: bool,
    first: bool = True,
) -> None:
    """Emit the member initializer list for a move constructor.

    Bloomberg's bas_codegen.pl uses two different styles:

    No-allocator variant — trailing-comma style:
        : d_foo(bsl::move(original.d_foo)),
          d_bar(bsl::move(original.d_bar))

    With-allocator variant — leading-comma BDE style:
        : d_foo(bsl::move(original.d_foo))
        , d_bar(bsl::move(original.d_bar))
    """
    if not sorted_resolved:
        return

    if with_allocator:
        # Leading-comma BDE style (same as copy/default constructors)
        for i, rf in enumerate(sorted_resolved):
            prefix = (": " if first else ", ") if i == 0 else ", "
            m = to_member_name(rf.field.name)
            if rf.needs_allocator:
                _emit_init_member(
                    w, prefix, m, f"bsl::move(original.{m}), basicAllocator"
                )
            else:
                _emit_init_member(w, prefix, m, f"bsl::move(original.{m})")
    else:
        # Trailing-comma style for noexcept move constructor
        last = len(sorted_resolved) - 1
        for i, rf in enumerate(sorted_resolved):
            prefix = ("  " if not first else ": ") if i == 0 else "  "
            suffix = "," if i < last else ""
            m = to_member_name(rf.field.name)
            _emit_init_member(w, prefix, m, f"bsl::move(original.{m})", suffix)


def _emit_noexcept_move_ctor(
    w: Writer, name: str, sorted_resolved: list[ResolvedField],
    alloc_prefix: str | None = None,
) -> None:
    """Emit the noexcept move constructor declaration + init list.

    Bloomberg uses four patterns depending on line length:

    Pattern A — inline (all ``{decl} noexcept : {init}`` lines ≤79)::

        SubId::SubId(SubId&& original) noexcept : d_appId(...),
                                                   d_subId(...)

    Pattern B — regular trailing-comma (``{decl} noexcept`` ≤79)::

        ClusterNode::ClusterNode(ClusterNode&& original) noexcept
        : d_hostName(...),
          d_dataCenter(...)

    Pattern C — continuation noexcept (``{decl}`` ≤79 and init fits)::

        ClientMsgGroupsCount::ClientMsgGroupsCount(ClientMsgGroupsCount&& original)
            noexcept : d_clientDescription(...),
                       d_numMsgGroupIds(...)

    Pattern D — wrapped declaration (``{decl}`` or init too long)::

        LeastRecentlyUsedGroupId::LeastRecentlyUsedGroupId(
            LeastRecentlyUsedGroupId&& original) noexcept
        : d_lastSeenDeltaNanoseconds(...),
          d_clientDescription(...)
    """
    decl = f"{name}::{name}({name}&& original)"
    decl_noexcept = f"{decl} noexcept"

    if not sorted_resolved:
        if len(decl_noexcept) <= 79:
            w.line(decl_noexcept)
        else:
            w.wrapped(decl_noexcept)
        return

    # Build member init strings with trailing commas
    inits: list[str] = []
    if alloc_prefix:
        inits.append(f"{alloc_prefix},")
    last = len(sorted_resolved) - 1
    for i, rf in enumerate(sorted_resolved):
        m = to_member_name(rf.field.name)
        suffix = "," if i < last else ""
        inits.append(f"{m}(bsl::move(original.{m})){suffix}")

    # Pattern A: Inline — {decl} noexcept : {first_init} all on one line
    inline_prefix = f"{decl_noexcept} : "
    align_a = " " * len(inline_prefix)
    inline_lines = [inline_prefix + inits[0]]
    for init in inits[1:]:
        inline_lines.append(align_a + init)
    if all(len(ln) <= 79 for ln in inline_lines):
        for ln in inline_lines:
            w.line(ln)
        return

    if len(decl_noexcept) <= 79:
        # Pattern B: Regular trailing-comma style on separate lines
        w.line(decl_noexcept)
        if alloc_prefix:
            w.line(f": {alloc_prefix},")
            _emit_move_init_list(w, sorted_resolved, with_allocator=False, first=False)
        else:
            _emit_move_init_list(w, sorted_resolved, with_allocator=False)
        return

    # decl_noexcept > 79 — try Pattern C if decl fits and cont lines fit
    if len(decl) <= 79:
        cont_prefix = "    noexcept : "
        align_c = " " * len(cont_prefix)
        cont_lines = [cont_prefix + inits[0]]
        for init in inits[1:]:
            cont_lines.append(align_c + init)
        if all(len(ln) <= 79 for ln in cont_lines):
            w.line(decl)
            for ln in cont_lines:
                w.line(ln)
            return

    # Pattern D: Wrap {decl} noexcept via w.wrapped(), then trailing-comma
    w.wrapped(decl_noexcept)
    _emit_move_init_list(w, sorted_resolved, with_allocator=False)


def _emit_placement_new(
    w: Writer, m: str, cpp_type: str, args: str, indent: str = "        "
) -> None:
    """Emit a placement new expression, wrapping when needed.

    Wrapping strategy (matches bas_codegen.pl / clang-format output):
    1. Single line if it fits.
    2. Split at ``buffer())``: ``Type(args);`` on one continuation line.
    3. Split at ``buffer())``, paren-aligned wrapping of constructor args
       (when ``Type(first_arg,`` fits on the continuation line).
    4. Keep ``Type(`` on first line, wrap args with continuation indent
       (when paren-aligned after split would exceed the column limit).
    """
    line = f"{indent}new ({m}.buffer()) {cpp_type}({args});"
    if len(line) <= 79:
        w.line(line)
        return

    cont_indent = indent + "    "
    arg_parts = args.split(", ") if ", " in args else [args]

    def _try_keep_type_on_new_line() -> bool:
        """Strategy: keep Type( on the new-expression line, wrap args."""
        first_part = f"{indent}new ({m}.buffer()) {cpp_type}("
        if len(first_part) > 79:
            return False
        all_fit = all(
            len(f"{cont_indent}{arg}{');' if i == len(arg_parts) - 1 else ','}") <= 79
            for i, arg in enumerate(arg_parts)
        )
        if not all_fit:
            return False
        w.line(first_part)
        for i, arg in enumerate(arg_parts):
            sfx = ");" if i == len(arg_parts) - 1 else ","
            w.line(f"{cont_indent}{arg}{sfx}")
        return True

    _CPP_BUILTINS = {"bool", "char", "short", "int", "long", "float", "double",
                      "unsigned", "signed"}
    # For C++ built-in types, Bloomberg keeps Type( on the new-expression
    # line before trying the buffer()) split.
    if cpp_type in _CPP_BUILTINS and _try_keep_type_on_new_line():
        return

    # Split at buffer()), Type(args) on one continuation line
    cont_single = f"{cont_indent}{cpp_type}({args});"
    if len(cont_single) <= 79:
        w.line(f"{indent}new ({m}.buffer())")
        w.line(cont_single)
        return

    # Split at buffer()), paren-aligned wrapping of args
    if len(arg_parts) > 1:
        first_cont = f"{cont_indent}{cpp_type}({arg_parts[0]},"
        if len(first_cont) <= 79:
            paren_col = len(cont_indent) + len(cpp_type) + 1
            paren_pad = " " * paren_col
            all_fit = all(
                len(
                    f"{paren_pad}{arg_parts[i]}"
                    f"{');' if i == len(arg_parts) - 1 else ','}"
                )
                <= 79
                for i in range(1, len(arg_parts))
            )
            if all_fit:
                w.line(f"{indent}new ({m}.buffer())")
                w.line(first_cont)
                for i in range(1, len(arg_parts)):
                    sfx = ");" if i == len(arg_parts) - 1 else ","
                    w.line(f"{paren_pad}{arg_parts[i]}{sfx}")
                return

    # Fallback: keep Type( on first line, wrap args
    if _try_keep_type_on_new_line():
        return

    # Last resort: split at buffer()) and wrap Type(args)
    w.line(f"{indent}new ({m}.buffer())")
    w.wrapped(cont_single)


# ---------------------------------------------------------------------------
# Choice implementation
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


_NO_DESTROY_TYPES = {
    "bool",
    "int",
    "unsigned int",
    "double",
    "bsls::Types::Int64",
    "bsls::Types::Uint64",
    "bdlt::DateTz",
    "bdlt::TimeTz",
    "bdlt::DatetimeTz",
}


def _emit_destructor_call(w: Writer, rf: ResolvedField, member: str) -> None:
    """Emit the appropriate destructor call for a choice selection in reset()."""
    cpp_type = rf.cpp_type
    if cpp_type in _NO_DESTROY_TYPES:
        w.line("        // no destruction required")
    elif "::" in cpp_type:
        # Types with :: need a typedef for the destructor call
        w.line(f"        typedef {cpp_type} Type;")
        w.line(f"        {member}.object().~Type();")
    else:
        w.line(f"        {member}.object().~{cpp_type}();")


def _emit_choice_impl(w: Writer, t: ComplexType, schema: Schema) -> None:
    """Emit .cpp implementation for a choice type."""
    name = to_class_name(t.name)
    has_alloc = _choice_needs_allocator(t, schema)
    resolved = [resolve_field(f, t, schema) for f in t.choices]
    n = len(t.choices)

    w.line()
    w.separator(name)
    w.line()
    w.line("// CONSTANTS")
    w.line()
    class_name_line = f'const char {name}::CLASS_NAME[] = "{name}";'
    if len(class_name_line) <= 79:
        w.line(class_name_line)
    else:
        w.line(f"const char {name}::CLASS_NAME[] =")
        w.line(f'    "{name}";')
    w.line()

    # SELECTION_INFO_ARRAY
    sel_decl = f"const bdlat_SelectionInfo {name}::SELECTION_INFO_ARRAY[] ="
    sel_decl_split = len(sel_decl + " {") > 79
    if not sel_decl_split:
        w.line(sel_decl + " {")
    else:
        w.line(sel_decl)
    for i, (rf, f) in enumerate(zip(resolved, t.choices)):
        is_last = i == n - 1
        sel_id = f"SELECTION_ID_{to_upper_snake(f.name)}"
        mode = f"bdlat_FormattingMode::{rf.formatting_mode}"
        close = "}};" if is_last else "},"
        if sel_decl_split:
            brace_prefix = "    {{" if i == 0 else "     {"
            field_indent = "      "
        else:
            brace_prefix = "    {"
            field_indent = "     "
        single = f'{brace_prefix}{sel_id}, "{f.name}", sizeof("{f.name}") - 1, "", {mode}{close}'
        if len(single) <= 79:
            w.line(single)
        else:
            w.line(f"{brace_prefix}{sel_id},")
            w.line(f'{field_indent}"{f.name}",')
            w.line(f'{field_indent}sizeof("{f.name}") - 1,')
            w.line(f'{field_indent}"",')
            if is_last:
                w.line(f"{field_indent}{mode}}}}};")
            else:
                w.line(f"{field_indent}{mode}}},")
    w.line()

    # CLASS METHODS
    w.line("// CLASS METHODS")
    w.line()
    w.wrapped(
        f"const bdlat_SelectionInfo* {name}::lookupSelectionInfo(const char* name, int nameLength)"
    )
    w.line("{")
    w.line(f"    for (int i = 0; i < {n}; ++i) {{")
    sel_assign = f"        const bdlat_SelectionInfo& selectionInfo = {name}::SELECTION_INFO_ARRAY[i];"
    if len(sel_assign) > 79:
        w.line("        const bdlat_SelectionInfo& selectionInfo =")
        w.line(f"            {name}::SELECTION_INFO_ARRAY[i];")
    else:
        w.line(sel_assign)
    w.line()
    w.line("        if (nameLength == selectionInfo.d_nameLength &&")
    w.line("            0 == bsl::memcmp(selectionInfo.d_name_p, name, nameLength)) {")
    w.line("            return &selectionInfo;")
    w.line("        }")
    w.line("    }")
    w.line()
    w.line("    return 0;")
    w.line("}")
    w.line()
    w.wrapped(f"const bdlat_SelectionInfo* {name}::lookupSelectionInfo(int id)")
    w.line("{")
    w.line("    switch (id) {")
    for f in t.choices:
        id_name = f"SELECTION_ID_{to_upper_snake(f.name)}"
        idx_name = f"SELECTION_INDEX_{to_upper_snake(f.name)}"
        single = f"    case {id_name}: return &SELECTION_INFO_ARRAY[{idx_name}];"
        if len(single) <= 79:
            w.line(single)
        else:
            w.line(f"    case {id_name}:")
            ret_line = f"        return &SELECTION_INFO_ARRAY[{idx_name}];"
            if len(ret_line) <= 79:
                w.line(ret_line)
            else:
                w.line("        return &SELECTION_INFO_ARRAY")
                w.line(f"            [{idx_name}];")
    w.line("    default: return 0;")
    w.line("    }")
    w.line("}")

    # CREATORS - copy constructor
    w.line()
    w.line("// CREATORS")
    w.line()
    if has_alloc:
        w.wrapped(
            f"{name}::{name}(const {name}& original, bslma::Allocator* basicAllocator)"
        )
        w.line(": d_selectionId(original.d_selectionId)")
        w.line(", d_allocator_p(bslma::Default::allocator(basicAllocator))")
    else:
        w.wrapped(f"{name}::{name}(const {name}& original)")
        w.line(": d_selectionId(original.d_selectionId)")
    w.line("{")
    w.line("    switch (d_selectionId) {")
    for rf in resolved:
        m = to_member_name(rf.field.name)
        sel_id = f"SELECTION_ID_{to_upper_snake(rf.field.name)}"
        if has_alloc and rf.needs_allocator:
            w.line(f"    case {sel_id}: {{")
            _emit_placement_new(
                w, m, rf.cpp_type, f"original.{m}.object(), d_allocator_p"
            )
            w.line("    } break;")
        else:
            w.line(f"    case {sel_id}: {{")
            _emit_placement_new(w, m, rf.cpp_type, f"original.{m}.object()")
            w.line("    } break;")
    w.line("    default: BSLS_ASSERT(SELECTION_ID_UNDEFINED == d_selectionId);")
    w.line("    }")
    w.line("}")
    w.line()

    # Move constructor
    w.line(
        "#if defined(BSLS_COMPILERFEATURES_SUPPORT_RVALUE_REFERENCES) &&               \\"
    )
    w.line("    defined(BSLS_COMPILERFEATURES_SUPPORT_NOEXCEPT)")
    decl = f"{name}::{name}({name}&& original)"
    decl_noexcept = f"{decl} noexcept"

    if len(decl_noexcept) <= 79:
        # noexcept fits on the declaration line
        w.line(decl_noexcept)
        if has_alloc:
            w.line(": d_selectionId(original.d_selectionId),")
            w.line("  d_allocator_p(original.d_allocator_p)")
        else:
            w.line(": d_selectionId(original.d_selectionId)")
    elif len(decl) <= 79:
        # Pattern C: decl fits but decl+noexcept doesn't — continuation
        w.line(decl)
        if has_alloc:
            w.line("    noexcept : d_selectionId(original.d_selectionId),")
            w.line("               d_allocator_p(original.d_allocator_p)")
        else:
            w.line("    noexcept : d_selectionId(original.d_selectionId)")
    else:
        # Pattern D: decl also overflows — wrap at param, noexcept on
        # param line, regular trailing-comma init list
        w.wrapped(decl_noexcept)
        if has_alloc:
            w.line(": d_selectionId(original.d_selectionId),")
            w.line("  d_allocator_p(original.d_allocator_p)")
        else:
            w.line(": d_selectionId(original.d_selectionId)")
    w.line("{")
    w.line("    switch (d_selectionId) {")
    for rf in resolved:
        m = to_member_name(rf.field.name)
        sel_id = f"SELECTION_ID_{to_upper_snake(rf.field.name)}"
        if has_alloc and rf.needs_allocator:
            w.line(f"    case {sel_id}: {{")
            _emit_placement_new(
                w, m, rf.cpp_type, f"bsl::move(original.{m}.object()), d_allocator_p"
            )
            w.line("    } break;")
        else:
            w.line(f"    case {sel_id}: {{")
            _emit_placement_new(w, m, rf.cpp_type, f"bsl::move(original.{m}.object())")
            w.line("    } break;")
    w.line("    default: BSLS_ASSERT(SELECTION_ID_UNDEFINED == d_selectionId);")
    w.line("    }")
    w.line("}")

    # Move constructor with allocator (alloc-aware only)
    if has_alloc:
        w.line()
        w.wrapped(
            f"{name}::{name}({name}&& original, bslma::Allocator* basicAllocator)"
        )
        w.line(": d_selectionId(original.d_selectionId)")
        w.line(", d_allocator_p(bslma::Default::allocator(basicAllocator))")
        w.line("{")
        w.line("    switch (d_selectionId) {")
        for rf in resolved:
            m = to_member_name(rf.field.name)
            sel_id = f"SELECTION_ID_{to_upper_snake(rf.field.name)}"
            if rf.needs_allocator:
                w.line(f"    case {sel_id}: {{")
                _emit_placement_new(
                    w,
                    m,
                    rf.cpp_type,
                    f"bsl::move(original.{m}.object()), d_allocator_p",
                )
                w.line("    } break;")
            else:
                w.line(f"    case {sel_id}: {{")
                _emit_placement_new(
                    w, m, rf.cpp_type, f"bsl::move(original.{m}.object())"
                )
                w.line("    } break;")
        w.line("    default: BSLS_ASSERT(SELECTION_ID_UNDEFINED == d_selectionId);")
        w.line("    }")
        w.line("}")

    w.line("#endif")
    w.line()

    # MANIPULATORS
    w.line("// MANIPULATORS")
    w.line()

    # Copy assignment
    w.wrapped(f"{name}& {name}::operator=(const {name}& rhs)")
    w.line("{")
    w.line("    if (this != &rhs) {")
    w.line("        switch (rhs.d_selectionId) {")
    for rf in resolved:
        m = to_member_name(rf.field.name)
        sel_id = f"SELECTION_ID_{to_upper_snake(rf.field.name)}"
        title = to_title_name(rf.field.name)
        w.line(f"        case {sel_id}: {{")
        make_line = f"            make{title}(rhs.{m}.object());"
        if len(make_line) <= 79:
            w.line(make_line)
        else:
            w.line(f"            make{title}(")
            w.line(f"                rhs.{m}.object());")
        w.line("        } break;")
    w.line("        default:")
    w.line("            BSLS_ASSERT(SELECTION_ID_UNDEFINED == rhs.d_selectionId);")
    w.line("            reset();")
    w.line("        }")
    w.line("    }")
    w.line()
    w.line("    return *this;")
    w.line("}")
    w.line()

    # Move assignment
    w.line(
        "#if defined(BSLS_COMPILERFEATURES_SUPPORT_RVALUE_REFERENCES) &&               \\"
    )
    w.line("    defined(BSLS_COMPILERFEATURES_SUPPORT_NOEXCEPT)")
    w.wrapped(f"{name}& {name}::operator=({name}&& rhs)")
    w.line("{")
    w.line("    if (this != &rhs) {")
    w.line("        switch (rhs.d_selectionId) {")
    for rf in resolved:
        m = to_member_name(rf.field.name)
        sel_id = f"SELECTION_ID_{to_upper_snake(rf.field.name)}"
        title = to_title_name(rf.field.name)
        w.line(f"        case {sel_id}: {{")
        make_line = f"            make{title}(bsl::move(rhs.{m}.object()));"
        if len(make_line) <= 79:
            w.line(make_line)
        else:
            w.line(f"            make{title}(")
            w.line(f"                bsl::move(rhs.{m}.object()));")
        w.line("        } break;")
    w.line("        default:")
    w.line("            BSLS_ASSERT(SELECTION_ID_UNDEFINED == rhs.d_selectionId);")
    w.line("            reset();")
    w.line("        }")
    w.line("    }")
    w.line()
    w.line("    return *this;")
    w.line("}")
    w.line("#endif")
    w.line()

    # reset()
    w.line(f"void {name}::reset()")
    w.line("{")
    w.line("    switch (d_selectionId) {")
    for rf in resolved:
        m = to_member_name(rf.field.name)
        sel_id = f"SELECTION_ID_{to_upper_snake(rf.field.name)}"
        w.line(f"    case {sel_id}: {{")
        _emit_destructor_call(w, rf, m)
        w.line("    } break;")
    w.line("    default: BSLS_ASSERT(SELECTION_ID_UNDEFINED == d_selectionId);")
    w.line("    }")
    w.line()
    w.line("    d_selectionId = SELECTION_ID_UNDEFINED;")
    w.line("}")
    w.line()

    # makeSelection(int)
    w.wrapped(f"int {name}::makeSelection(int selectionId)")
    w.line("{")
    w.line("    switch (selectionId) {")
    for rf in resolved:
        sel_id = f"SELECTION_ID_{to_upper_snake(rf.field.name)}"
        title = to_title_name(rf.field.name)
        w.line(f"    case {sel_id}: {{")
        w.line(f"        make{title}();")
        w.line("    } break;")
    w.line("    case SELECTION_ID_UNDEFINED: {")
    w.line("        reset();")
    w.line("    } break;")
    w.line("    default: return -1;")
    w.line("    }")
    w.line("    return 0;")
    w.line("}")
    w.line()

    # makeSelection(name, len)
    w.wrapped(f"int {name}::makeSelection(const char* name, int nameLength)")
    w.line("{")
    sel_lookup = "    const bdlat_SelectionInfo* selectionInfo = lookupSelectionInfo(name, nameLength);"
    if len(sel_lookup) > 79:
        first_part = (
            "    const bdlat_SelectionInfo* selectionInfo = lookupSelectionInfo(name,"
        )
        paren_col = first_part.index("(name") + 1
        w.line(first_part)
        w.line(f"{' ' * paren_col}nameLength);")
    else:
        w.line(sel_lookup)
    w.line("    if (0 == selectionInfo) {")
    w.line("        return -1;")
    w.line("    }")
    w.line()
    w.line("    return makeSelection(selectionInfo->d_id);")
    w.line("}")
    w.line()

    # make<Name>() methods
    for rf in resolved:
        fname = rf.field.name
        title = to_title_name(fname)
        m = to_member_name(fname)
        sel_id = f"SELECTION_ID_{to_upper_snake(fname)}"

        # Default make
        w.wrapped(f"{rf.cpp_type}& {name}::make{title}()")
        w.line("{")
        w.line(f"    if ({sel_id} == d_selectionId) {{")
        reset_line = f"        bdlat_ValueTypeFunctions::reset(&{m}.object());"
        if len(reset_line) <= 79:
            w.line(reset_line)
        else:
            w.line("        bdlat_ValueTypeFunctions::reset(")
            w.line(f"            &{m}.object());")
        w.line("    }")
        w.line("    else {")
        w.line("        reset();")
        if has_alloc and rf.needs_allocator:
            _emit_placement_new(w, m, rf.cpp_type, "d_allocator_p")
        elif rf.is_enum:
            _emit_placement_new(
                w, m, rf.cpp_type, f"static_cast<{rf.cpp_type}>(0)"
            )
        else:
            _emit_placement_new(w, m, rf.cpp_type, "")
        w.line(f"        d_selectionId = {sel_id};")
        w.line("    }")
        w.line()
        w.line(f"    return {m}.object();")
        w.line("}")
        w.line()

        # Copy make (fundamental types and enums use pass-by-value)
        is_fundamental = rf.cpp_type in _FUNDAMENTAL_CPP_TYPES or rf.is_enum
        if is_fundamental:
            w.wrapped(f"{rf.cpp_type}& {name}::make{title}({rf.cpp_type} value)")
        else:
            w.wrapped(f"{rf.cpp_type}& {name}::make{title}(const {rf.cpp_type}& value)")
        w.line("{")
        w.line(f"    if ({sel_id} == d_selectionId) {{")
        w.line(f"        {m}.object() = value;")
        w.line("    }")
        w.line("    else {")
        w.line("        reset();")
        if has_alloc and rf.needs_allocator:
            _emit_placement_new(w, m, rf.cpp_type, "value, d_allocator_p")
        else:
            _emit_placement_new(w, m, rf.cpp_type, "value")
        w.line(f"        d_selectionId = {sel_id};")
        w.line("    }")
        w.line()
        w.line(f"    return {m}.object();")
        w.line("}")
        w.line()

        # Move make (skip for fundamental types — no benefit to moving)
        if not is_fundamental:
            w.line(
                "#if defined(BSLS_COMPILERFEATURES_SUPPORT_RVALUE_REFERENCES) &&               \\"
            )
            w.line("    defined(BSLS_COMPILERFEATURES_SUPPORT_NOEXCEPT)")
            w.wrapped(f"{rf.cpp_type}& {name}::make{title}({rf.cpp_type}&& value)")
            w.line("{")
            w.line(f"    if ({sel_id} == d_selectionId) {{")
            w.line(f"        {m}.object() = bsl::move(value);")
            w.line("    }")
            w.line("    else {")
            w.line("        reset();")
            if has_alloc and rf.needs_allocator:
                _emit_placement_new(
                    w, m, rf.cpp_type, "bsl::move(value), d_allocator_p"
                )
            else:
                _emit_placement_new(w, m, rf.cpp_type, "bsl::move(value)")
            w.line(f"        d_selectionId = {sel_id};")
            w.line("    }")
            w.line()
            w.line(f"    return {m}.object();")
            w.line("}")
            w.line("#endif")
            w.line()

    # ACCESSORS - print
    w.line("// ACCESSORS")
    w.line()
    w.wrapped(
        f"bsl::ostream& {name}::print(bsl::ostream& stream, int level, int spacesPerLevel) const"
    )
    w.line("{")
    w.line("    bslim::Printer printer(&stream, level, spacesPerLevel);")
    w.line("    printer.start();")
    w.line("    switch (d_selectionId) {")
    for rf in resolved:
        m = to_member_name(rf.field.name)
        sel_id = f"SELECTION_ID_{to_upper_snake(rf.field.name)}"
        w.line(f"    case {sel_id}: {{")
        acc = to_accessor_name(rf.field.name)
        pa_line = f'        printer.printAttribute("{acc}", {m}.object());'
        if len(pa_line) <= 79:
            w.line(pa_line)
        else:
            paren_col = len("        printer.printAttribute(")
            w.line(f'        printer.printAttribute("{acc}",')
            w.line(f"{' ' * paren_col}{m}.object());")
        w.line("    } break;")
    w.line('    default: stream << "SELECTION UNDEFINED\\n";')
    w.line("    }")
    w.line("    printer.end();")
    w.line("    return stream;")
    w.line("}")
    w.line()

    # selectionName
    w.wrapped(f"const char* {name}::selectionName() const")
    w.line("{")
    w.line("    switch (d_selectionId) {")
    for rf in resolved:
        sel_id = f"SELECTION_ID_{to_upper_snake(rf.field.name)}"
        idx_name = f"SELECTION_INDEX_{to_upper_snake(rf.field.name)}"
        w.line(f"    case {sel_id}:")
        ret_line = f"        return SELECTION_INFO_ARRAY[{idx_name}].name();"
        if len(ret_line) <= 79:
            w.line(ret_line)
        else:
            # Check if ARRAY[idx] fits on one line
            arr_line = f"        return SELECTION_INFO_ARRAY[{idx_name}]"
            if len(arr_line) <= 79:
                w.line(arr_line)
                w.line("            .name();")
            else:
                w.line("        return SELECTION_INFO_ARRAY")
                w.line(f"            [{idx_name}]")
                w.line("                .name();")
    w.line("    default:")
    w.line("        BSLS_ASSERT(SELECTION_ID_UNDEFINED == d_selectionId);")
    w.line('        return "(* UNDEFINED *)";')
    w.line("    }")
    w.line("}")


# ---------------------------------------------------------------------------
# Top-level emission
# ---------------------------------------------------------------------------


def emit_source(  # pylint: disable=redefined-builtin
    schema: Schema, pkg: str, component: str, copyright: str = "",
    xsd_name: str = "", codegen_version: str = "", extra_flags: str = "",
) -> str:
    """Generate the complete .cpp file content.

    If *copyright* is provided it is emitted verbatim before the
    ``*DO NOT EDIT*`` banner (matching the ``codegen.sh`` pipeline).
    """
    result = expand_hybrids(schema)
    schema = result.schema
    hybrid_meta = result.hybrid_choice_names

    w = Writer()
    component_full = f"{pkg}_{component}"

    if copyright:
        w.raw(copyright.rstrip("\n"))
        w.line()
    w.line(make_banner(component_full, "cpp"))
    w.line()
    w.line(f"#include <{component_full}.h>")
    w.line()
    w.raw(_build_includes_cpp(schema))
    w.line()
    w.line("namespace BloombergLP {")
    w.line(f"namespace {pkg} {{")

    # Type implementations — unified topological ordering (enums + classes)
    enum_map = {e.name: e for e in schema.enums}
    unified_levels = topological_sort_types(schema, include_enums=True)
    for level in unified_levels:
        for type_name in level:
            if type_name in enum_map:
                _emit_enum_impl(w, enum_map[type_name])
            else:
                t = schema.type_by_name(type_name)
                if not isinstance(t, ComplexType):
                    continue
                if t.kind == TypeKind.SEQUENCE:
                    _emit_sequence_impl(w, t, schema, hybrid_meta)
                elif t.kind == TypeKind.CHOICE:
                    _emit_choice_impl(w, t, schema)

    w.line("}  // close package namespace")
    w.line("}  // close enterprise namespace")
    w.line()
    xsd_file = xsd_name or f"{pkg}.xsd"
    ver = codegen_version or "2025.11.13"
    flags_suffix = f" {extra_flags}" if extra_flags else ""
    w.line(f"// GENERATED BY BLP_BAS_CODEGEN_{ver}")
    w.line("// USING bas_codegen.pl -m msg --noAggregateConversion --noExternalization")
    w.line(f"// --noIdent --package {pkg} --msgComponent {component} {xsd_file}{flags_suffix}")

    return w.result()
