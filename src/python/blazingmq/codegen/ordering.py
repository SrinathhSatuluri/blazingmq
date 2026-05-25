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

"""Member ordering by alignment and forward-declaration topological sort."""

from __future__ import annotations


from .model import (
    ComplexType,
    EnumType,
    Field,
    FieldKind,
    Schema,
    XSD_TYPE_MAP,
)


def _alignment_bucket(f: Field, schema: Schema) -> int:
    """Return the alignment bucket for member ordering.

    Buckets (higher = earlier in declaration, sorted descending):
      8: pointer-aligned types (Int64, Uint64, double, DatetimeTz, string,
         vector, custom structs, NullableValue of 8-bucket types)
      4: 4-byte types (int, unsigned int, enum::Value)
      1: 1-byte types (bool, NullableValue<bool>)

    This matches the bas_codegen.pl member layout convention.
    """
    type_name = f.type_name

    # Arrays are always bsl::vector — 8-aligned
    if f.kind == FieldKind.ARRAY:
        return 8

    # Optional types: inherit the bucket of the wrapped type
    if f.kind == FieldKind.OPTIONAL:
        # Determine the bucket of the underlying type
        if type_name in XSD_TYPE_MAP:
            _, size, _ = XSD_TYPE_MAP[type_name]
            if size >= 8:
                return 8
            if size >= 4:
                return 4
            return 1
        # Custom optional type — check what it wraps
        ref_name = type_name.removeprefix("tns:")
        resolved = schema.type_by_name(ref_name)
        if isinstance(resolved, EnumType):
            return 4
        return 8  # Custom struct optional

    # Primitive XSD types
    if type_name in XSD_TYPE_MAP:
        _, size, _ = XSD_TYPE_MAP[type_name]
        if size >= 8:
            return 8
        if size >= 4:
            return 4
        return 1

    # Custom type reference
    ref_name = type_name.removeprefix("tns:")
    resolved = schema.type_by_name(ref_name)
    if isinstance(resolved, EnumType):
        return 4  # Enums are int-sized

    return 8  # Complex types are pointer-aligned


def _desc_type_key(decl_type: str) -> tuple:
    """Sort key for descending lexicographic order of C++ type names.

    Returns a tuple of negated character ordinals, with a 0 sentinel
    so that longer strings with the same prefix sort first
    (e.g. "QueueState" before "Queue").
    """
    return tuple(-ord(c) for c in decl_type) + (0,)


def sort_fields_by_alignment(
    fields: list[Field], schema: Schema, containing_type: ComplexType | None = None
) -> list[Field]:
    """Sort fields for BDE-compliant struct member layout.

    Sort key: alignment bucket DESC, C++ decl_type name DESC, XSD index ASC.
    This matches the member ordering produced by bas_codegen.pl.
    """
    if containing_type is None:
        # Fallback: find containing type by checking field membership
        for t in schema.types:
            if fields and fields[0] in t.fields:
                containing_type = t
                break

    if containing_type is None:
        return list(fields)  # Can't resolve — keep original order

    from .resolver import resolve_field  # pylint: disable=import-outside-toplevel

    indexed = []
    for xsd_idx, f in enumerate(fields):
        rf = resolve_field(f, containing_type, schema)
        bucket = _alignment_bucket(f, schema)
        indexed.append((bucket, rf.decl_type, xsd_idx, f))

    indexed.sort(key=lambda x: (-x[0], _desc_type_key(x[1]), x[2]))
    return [item[3] for item in indexed]


def _collect_dependencies(t: ComplexType, _schema: Schema) -> set[str]:
    """Collect the set of type names that `t` depends on (direct references).

    Self-references are excluded — they use NullableAllocatedValue (pointer)
    which only requires a forward declaration, not the full definition.
    """
    deps: set[str] = set()
    all_fields = t.fields + t.choices
    for f in all_fields:
        ref = f.type_name.removeprefix("tns:")
        if ref != f.type_name:  # Was indeed a tns: reference
            if ref != t.name:  # Skip self-references
                deps.add(ref)
    return deps


def topological_sort_types(
    schema: Schema, *, include_enums: bool = False
) -> list[list[str]]:
    """Group types by dependency depth for forward declaration ordering.

    Returns a list of levels. Level 0 = types with no dependencies on other
    custom types or enums. Level 1 = types that depend only on level-0 types
    or enums. Etc.  Within each level, names are sorted alphabetically.

    Enums participate in the topological sort (as leaf nodes with no deps).
    By default they are filtered from the output.  Pass *include_enums=True*
    to keep them in the levels (they always land at level 0).
    """
    # Build name -> set of dependencies (all tns: refs: complex types + enums)
    complex_names = {t.name for t in schema.types}
    enum_names = {e.name for e in schema.enums}
    all_type_names = complex_names | enum_names

    deps_map: dict[str, set[str]] = {}
    for t in schema.types:
        deps = _collect_dependencies(t, schema) & all_type_names
        deps_map[t.name] = deps

    # Add enums as leaf nodes (no dependencies)
    for name in enum_names:
        deps_map[name] = set()

    levels: list[list[str]] = []
    assigned: set[str] = set()

    # Kahn's algorithm variant: repeatedly find types whose deps are all assigned
    remaining = set(deps_map.keys())
    while remaining:
        level = []
        for name in sorted(remaining):
            if deps_map[name] <= assigned:
                level.append(name)
        if not level:
            # Circular dependency — assign all remaining to this level
            level = sorted(remaining)
        for name in level:
            remaining.discard(name)
            assigned.add(name)
        # Filter out enum names unless requested
        if include_enums:
            filtered = sorted(level)
        else:
            filtered = sorted(n for n in level if n in complex_names)
        if filtered:
            levels.append(filtered)

    return levels
