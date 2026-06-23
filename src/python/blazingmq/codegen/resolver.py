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

"""Resolve XSD fields to C++ types, determine allocator needs, formatting modes."""

from __future__ import annotations

from dataclasses import dataclass

from .model import (
    ComplexType,
    EnumType,
    Field,
    FieldKind,
    Schema,
    XSD_TYPE_MAP,
)
from .naming import to_accessor_name, to_class_name


@dataclass
class ResolvedField:
    """A field with its resolved C++ type information."""

    field: Field
    cpp_type: str  # The C++ storage type (e.g. "bsl::string", "int")
    decl_type: str  # The declared type in class body (may include wrapper)
    needs_allocator: bool  # Whether this field's type needs an allocator
    formatting_mode: str  # e.g. "e_TEXT", "e_DEC", "e_DEFAULT"
    is_nullable_allocated: bool = False  # Uses NullableAllocatedValue (self-ref)
    is_enum: bool = False  # True when the field's type is an enumeration

    @property
    def name(self) -> str:
        """Accessor/method name — XSD element name with first char lowercased."""
        return to_accessor_name(self.field.name)


def _is_allocator_aware_type(type_name: str, schema: Schema) -> bool:
    """Determine if a type requires an allocator parameter."""
    if type_name in XSD_TYPE_MAP:
        cpp_type, _, _ = XSD_TYPE_MAP[type_name]
        return cpp_type in ("bsl::string", "bsl::vector<char>")

    ref_name = type_name.removeprefix("tns:")
    resolved = schema.type_by_name(ref_name)

    if isinstance(resolved, EnumType):
        return False

    if isinstance(resolved, ComplexType):
        return type_needs_allocator(resolved, schema)

    # Unknown type — assume allocator-aware to be safe
    return True


def type_needs_allocator(
    t: ComplexType, schema: Schema, _visited: set[str] | None = None
) -> bool:
    """Determine if a ComplexType needs an allocator.

    A type needs an allocator if ANY of its fields:
    - Is a bsl::string
    - Is a bsl::vector<T>
    - Is a bdlb::NullableValue/NullableAllocatedValue of an allocator-aware type
    - Is another allocator-aware ComplexType

    Uses a visited set to guard against infinite recursion on circular types.
    """
    if _visited is None:
        _visited = set()
    if t.name in _visited:
        return False
    _visited.add(t.name)

    all_fields = t.fields + t.choices

    for f in all_fields:
        # Arrays always need allocator (bsl::vector)
        if f.kind == FieldKind.ARRAY:
            return True

        # Check the base type
        if f.type_name in XSD_TYPE_MAP:
            cpp_type, _, _ = XSD_TYPE_MAP[f.type_name]
            if cpp_type == "bsl::string":
                return True
        else:
            # Custom type reference
            ref_name = f.type_name.removeprefix("tns:")
            resolved = schema.type_by_name(ref_name)
            if isinstance(resolved, EnumType):
                continue
            if isinstance(resolved, ComplexType):
                if type_needs_allocator(resolved, schema, _visited):
                    return True

    return False


def _formatting_mode(f: Field, schema: Schema) -> str:
    """Determine the bdlat_FormattingMode for a field."""
    if f.type_name in XSD_TYPE_MAP:
        _, _, mode = XSD_TYPE_MAP[f.type_name]
        return f"e_{mode}"

    ref_name = f.type_name.removeprefix("tns:")
    resolved = schema.type_by_name(ref_name)

    if isinstance(resolved, EnumType):
        return "e_DEFAULT"

    # Complex types use e_DEFAULT
    return "e_DEFAULT"


def _has_default_value(f: Field) -> bool:
    """Check if a field has a DEFAULT_INITIALIZER modifier."""
    return f.default is not None


def resolve_field(
    f: Field, containing_type: ComplexType, schema: Schema
) -> ResolvedField:
    """Resolve a single field to its full C++ representation."""
    # Get base C++ type
    is_enum = False
    if f.type_name in XSD_TYPE_MAP:
        base_cpp, _, _ = XSD_TYPE_MAP[f.type_name]
    else:
        ref_name = f.type_name.removeprefix("tns:")
        resolved_type = schema.type_by_name(ref_name)
        base_cpp = to_class_name(ref_name)
        if isinstance(resolved_type, EnumType):
            is_enum = True
            base_cpp = f"{base_cpp}::Value"

    needs_alloc = _is_allocator_aware_type(f.type_name, schema)
    fmt_mode = _formatting_mode(f, schema)
    is_nullable_alloc = False

    # Determine the declared storage type
    if f.kind == FieldKind.ARRAY:
        decl_type = f"bsl::vector<{base_cpp}>"
        needs_alloc = True
    elif f.kind == FieldKind.OPTIONAL:
        # Check for self-referential types (use NullableAllocatedValue)
        ref_name = f.type_name.removeprefix("tns:")
        if _is_self_referential(ref_name, containing_type, schema):
            decl_type = f"bdlb::NullableAllocatedValue<{base_cpp}>"
            is_nullable_alloc = True
            needs_alloc = True
        else:
            decl_type = f"bdlb::NullableValue<{base_cpp}>"
            if needs_alloc:
                pass  # Already True
    else:
        decl_type = base_cpp

    # BDE C++03 compatibility: avoid ">>" in nested templates
    decl_type = decl_type.replace(">>", "> >")

    return ResolvedField(
        field=f,
        cpp_type=base_cpp,
        decl_type=decl_type,
        needs_allocator=needs_alloc,
        formatting_mode=fmt_mode,
        is_nullable_allocated=is_nullable_alloc,
        is_enum=is_enum,
    )


def _is_self_referential(
    ref_name: str, containing: ComplexType, schema: Schema
) -> bool:
    """Check if ref_name can reach containing type (creating a cycle).

    In BlazingMQ schemas, self-referential types (e.g. CapacityMeter.parent
    which is NullableAllocatedValue<CapacityMeter>) must use
    NullableAllocatedValue to break the cycle.
    """
    if ref_name == containing.name:
        return True

    # Check indirect cycles (type A has optional B, B has optional A)
    visited: set[str] = set()
    return _reaches_type(ref_name, containing.name, schema, visited)


def _reaches_type(
    from_name: str, target: str, schema: Schema, visited: set[str]
) -> bool:
    """DFS: can we reach `target` from `from_name` through type references?"""
    if from_name in visited:
        return False
    visited.add(from_name)

    resolved = schema.type_by_name(from_name)
    if not isinstance(resolved, ComplexType):
        return False

    for f in resolved.fields + resolved.choices:
        if f.kind != FieldKind.OPTIONAL:
            continue
        ref = f.type_name.removeprefix("tns:")
        if ref == f.type_name:
            continue  # Not a tns: ref
        if ref == target:
            return True
        if _reaches_type(ref, target, schema, visited):
            return True

    return False
