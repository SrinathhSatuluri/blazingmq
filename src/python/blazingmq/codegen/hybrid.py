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

"""Pre-process hybrid types into sequence + choice pairs."""

from __future__ import annotations

from dataclasses import dataclass

from .model import ComplexType, Field, FieldKind, Schema, TypeKind


@dataclass
class HybridResult:
    """Result of expanding hybrid types in a schema.

    Attributes:
        schema: The transformed schema with hybrids split into
            separate sequence + choice types.
        hybrid_choice_names: Maps each expanded sequence type name
            to the list of original choice selection field names.
            Empty if no hybrids were present.
    """

    schema: Schema
    hybrid_choice_names: dict[str, list[str]]

    def get_choice_selections(self, type_name: str) -> list[str] | None:
        """Return choice selection names if this type was expanded from a hybrid."""
        return self.hybrid_choice_names.get(type_name)


def expand_hybrids(schema: Schema) -> HybridResult:
    """Expand hybrid types into separate sequence + choice types.

    For each hybrid type Foo with fields [f1, f2] and choices [c1, c2]:
    - Creates a new choice type 'FooChoice' with choices [c1, c2]
    - Converts Foo to a pure SEQUENCE with fields [f1, f2, choice]
      where 'choice' is a REGULAR field of type tns:FooChoice

    The choice field has special formatting (e_UNTAGGED) — handled in emitters.
    Returns a HybridResult containing the expanded schema and metadata.
    Idempotent: if the schema contains no HYBRID types the existing
    schema is returned unchanged with empty metadata.
    """
    if not any(t.kind == TypeKind.HYBRID for t in schema.types):
        return HybridResult(schema=schema, hybrid_choice_names={})

    hybrid_choice_names: dict[str, list[str]] = {}
    new_types: list[ComplexType] = []

    for t in schema.types:
        if t.kind != TypeKind.HYBRID:
            new_types.append(t)
            continue

        # Create the FooChoice type
        choice_name = f"{t.name}Choice"
        choice_type = ComplexType(
            name=choice_name,
            kind=TypeKind.CHOICE,
            doc="",
            choices=list(t.choices),
        )
        new_types.append(choice_type)

        # Create a synthetic "choice" field pointing to FooChoice
        choice_field = Field(
            name="choice",
            type_name=f"tns:{choice_name}",
            kind=FieldKind.REGULAR,
        )

        # Insert choice field at the correct position (from XSD ordering)
        fields = list(t.fields)
        fields.insert(t.choice_index, choice_field)

        # Convert hybrid to pure sequence
        seq_type = ComplexType(
            name=t.name,
            kind=TypeKind.SEQUENCE,
            doc=t.doc,
            fields=fields,
        )
        new_types.append(seq_type)

        # Record the hybrid metadata for special lookupAttributeInfo handling
        hybrid_choice_names[t.name] = [f.name for f in t.choices]

    return HybridResult(
        schema=Schema(enums=list(schema.enums), types=new_types),
        hybrid_choice_names=hybrid_choice_names,
    )
