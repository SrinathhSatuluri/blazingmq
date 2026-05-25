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

"""Parse a BlazingMQ XSD file into the internal model."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

from .model import (
    ComplexType,
    EnumType,
    EnumValue,
    Field,
    FieldKind,
    Schema,
    TypeKind,
)

# XML namespace map
NS = {
    "xs": "http://www.w3.org/2001/XMLSchema",
    "bdem": "http://bloomberg.com/schemas/bdem",
    "tns": "http://bloomberg.com/schemas/schema",
}

# Convenience: the XSD default namespace IS xs
XS = "{http://www.w3.org/2001/XMLSchema}"
BDEM = "{http://bloomberg.com/schemas/bdem}"


def _get_doc(elem: ET.Element) -> str:
    """Extract documentation text from an annotation child, if present."""
    ann = elem.find(f"{XS}annotation")
    if ann is None:
        return ""
    doc = ann.find(f"{XS}documentation")
    if doc is None or doc.text is None:
        return ""
    # Normalize whitespace but preserve paragraph breaks
    lines = doc.text.strip().splitlines()
    return "\n".join(line.strip() for line in lines)


def _parse_field(elem: ET.Element) -> Field:
    """Parse a single <element> into a Field."""
    name = elem.get("name", "")
    type_name = elem.get("type", "")
    min_occurs = elem.get("minOccurs", "1")
    max_occurs = elem.get("maxOccurs", "1")
    default = elem.get("default")
    bdem_id_str = elem.get(f"{BDEM}id")
    bdem_id = int(bdem_id_str) if bdem_id_str is not None else None

    if max_occurs == "unbounded":
        kind = FieldKind.ARRAY
    elif min_occurs == "0":
        kind = FieldKind.OPTIONAL
    else:
        kind = FieldKind.REGULAR

    return Field(
        name=name,
        type_name=type_name,
        kind=kind,
        default=default,
        doc=_get_doc(elem),
        id=bdem_id,
    )


def _parse_enum(elem: ET.Element) -> EnumType:
    """Parse a <simpleType> with restriction/enumeration into an EnumType."""
    name = elem.get("name", "")
    doc = _get_doc(elem)
    restriction = elem.find(f"{XS}restriction")
    values = []
    if restriction is not None:
        for i, enum_elem in enumerate(restriction.findall(f"{XS}enumeration")):
            val_name = enum_elem.get("value", "")
            bdem_id = enum_elem.get(f"{BDEM}id")
            val_id = int(bdem_id) if bdem_id is not None else i
            values.append(EnumValue(name=val_name, id=val_id))
    return EnumType(name=name, doc=doc, values=values)


def _extract_inline_enums(
    elements: list[ET.Element],
    parent_name: str,
    is_choice: bool,
) -> list[EnumType]:
    """Extract anonymous inline <simpleType> enums from elements.

    When an <element> contains an inline <simpleType> with <restriction>/<enumeration>,
    Bloomberg's codegen extracts it as a named enum type.  The naming convention:
    - For a choice element inside a hybrid: {ParentName}Choice{FieldNameCapitalized}
    - For other contexts: {ParentName}{FieldNameCapitalized}

    Updates each element's 'type' attribute to reference the new named type.
    Returns the list of extracted EnumType objects.
    """
    extracted: list[EnumType] = []
    for elem in elements:
        inline_st = elem.find(f"{XS}simpleType")
        if inline_st is None:
            continue
        restriction = inline_st.find(f"{XS}restriction")
        if restriction is None:
            continue
        enum_values = restriction.findall(f"{XS}enumeration")
        if not enum_values:
            continue

        field_name = elem.get("name", "")
        # Build the synthetic enum name following Bloomberg convention
        if is_choice:
            enum_name = f"{parent_name}Choice{field_name[0].upper()}{field_name[1:]}"
        else:
            enum_name = f"{parent_name}{field_name[0].upper()}{field_name[1:]}"

        values = []
        has_explicit_ids = False
        for i, ev in enumerate(enum_values):
            val_name = ev.get("value", "")
            bdem_id = ev.get(f"{BDEM}id")
            if bdem_id is not None:
                has_explicit_ids = True
            val_id = int(bdem_id) if bdem_id is not None else i
            values.append(EnumValue(name=val_name, id=val_id))

        # Inline enums without explicit bdem:id are sorted alphabetically
        # by Bloomberg's codegen; named enums preserve XSD order.
        if not has_explicit_ids:
            values.sort(key=lambda v: v.name)
            for i, v in enumerate(values):
                v.id = i

        extracted.append(EnumType(name=enum_name, doc="", values=values))
        # Update the element to reference the new named type
        elem.set("type", f"tns:{enum_name}")

    return extracted


def _parse_complex(elem: ET.Element) -> tuple[ComplexType, list[EnumType]]:
    """Parse a <complexType> into a ComplexType (sequence, choice, or hybrid).

    Returns a tuple of (ComplexType, list of extracted inline EnumTypes).
    """
    name = elem.get("name", "")
    doc = _get_doc(elem)
    seq = elem.find(f"{XS}sequence")
    choice_elem = elem.find(f"{XS}choice")
    inline_enums: list[EnumType] = []

    # Pure choice at top level
    if choice_elem is not None and seq is None:
        choice_elements = choice_elem.findall(f"{XS}element")
        inline_enums.extend(
            _extract_inline_enums(choice_elements, name, is_choice=True)
        )
        choices = [_parse_field(e) for e in choice_elements]
        return ComplexType(
            name=name, kind=TypeKind.CHOICE, doc=doc, choices=choices
        ), inline_enums

    if seq is None:
        # Empty sequence (shouldn't happen in practice but handle gracefully)
        return ComplexType(name=name, kind=TypeKind.SEQUENCE, doc=doc), inline_enums

    # Check for embedded choice within sequence (hybrid type)
    inner_choice = seq.find(f"{XS}choice")
    if inner_choice is not None:
        choice_elements = inner_choice.findall(f"{XS}element")
        inline_enums.extend(
            _extract_inline_enums(choice_elements, name, is_choice=True)
        )
        choices = [_parse_field(e) for e in choice_elements]
        # Determine position of <choice> among sibling elements
        # by counting <element> children that appear before <choice>
        choice_index = 0
        for child in seq:
            if child.tag == f"{XS}choice":
                break
            if child.tag == f"{XS}element":
                choice_index += 1
        seq_elements = seq.findall(f"{XS}element")
        inline_enums.extend(_extract_inline_enums(seq_elements, name, is_choice=False))
        fields = [_parse_field(e) for e in seq_elements]
        ct = ComplexType(
            name=name,
            kind=TypeKind.HYBRID,
            doc=doc,
            fields=fields,
            choices=choices,
            choice_index=choice_index,
        )
        return ct, inline_enums

    # Pure sequence
    seq_elements = seq.findall(f"{XS}element")
    inline_enums.extend(_extract_inline_enums(seq_elements, name, is_choice=False))
    fields = [_parse_field(e) for e in seq_elements]
    return ComplexType(
        name=name, kind=TypeKind.SEQUENCE, doc=doc, fields=fields
    ), inline_enums


def _normalize_type_refs(schema: Schema) -> None:
    """Normalize type references for consistent lookup.

    Handles quirks in BlazingMQ XSDs:
    1. xs:Foo for custom types -> tns:Foo
    2. Unprefixed built-in types (e.g. 'long') -> xs:long
    3. Package-prefixed types (e.g. 'mqbconfm:Foo') -> tns:Foo
    """
    from .model import XSD_TYPE_MAP  # pylint: disable=import-outside-toplevel

    known_names = {e.name for e in schema.enums} | {t.name for t in schema.types}
    # Built-in type names without prefix
    builtin_names = {k.removeprefix("xs:") for k in XSD_TYPE_MAP}

    def fix(f: Field) -> None:
        if f.type_name.startswith("tns:"):
            # tns:Foo where Foo is actually a built-in XSD type (XSD authoring mistake)
            local = f.type_name.removeprefix("tns:")
            if local not in known_names and local in builtin_names:
                f.type_name = f"xs:{local}"
        elif f.type_name.startswith("xs:"):
            # xs:Foo where Foo is a schema-local type
            local = f.type_name.removeprefix("xs:")
            if f.type_name not in XSD_TYPE_MAP and local in known_names:
                f.type_name = f"tns:{local}"
        elif ":" not in f.type_name:
            # Unprefixed type -- is it a built-in or a schema-local type?
            if f.type_name in builtin_names:
                f.type_name = f"xs:{f.type_name}"
            elif f.type_name in known_names:
                f.type_name = f"tns:{f.type_name}"
        else:
            # Non-standard namespace prefix (e.g. 'mqbconfm:Foo') --
            # strip the prefix and treat as a local or built-in type.
            local = f.type_name.split(":", 1)[1]
            if local in known_names:
                f.type_name = f"tns:{local}"
            elif local in builtin_names:
                f.type_name = f"xs:{local}"

    for t in schema.types:
        for f in t.fields:
            fix(f)
        for f in t.choices:
            fix(f)


def parse(path: str | Path) -> Schema:
    """Parse an XSD file and return a Schema model."""
    tree = ET.parse(path)
    root = tree.getroot()

    schema = Schema()

    for child in root:
        tag = child.tag.removeprefix(XS)
        if tag == "simpleType":
            schema.enums.append(_parse_enum(child))
        elif tag == "complexType":
            ct, inline_enums = _parse_complex(child)
            schema.enums.extend(inline_enums)
            schema.types.append(ct)

    _normalize_type_refs(schema)
    return schema
