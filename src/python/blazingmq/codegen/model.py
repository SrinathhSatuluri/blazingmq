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

"""Internal model representing parsed XSD types."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto


class TypeKind(Enum):
    SEQUENCE = auto()
    CHOICE = auto()
    HYBRID = auto()  # sequence with an embedded choice


class FieldKind(Enum):
    REGULAR = auto()
    OPTIONAL = auto()  # minOccurs="0" maxOccurs="1"
    ARRAY = auto()  # maxOccurs="unbounded"


# XSD base type -> (C++ type, sizeof, formatting mode suffix)
XSD_TYPE_MAP: dict[str, tuple[str, int, str]] = {
    "xs:string": ("bsl::string", 32, "TEXT"),
    "xs:int": ("int", 4, "DEC"),
    "xs:integer": ("bsls::Types::Int64", 8, "DEC"),
    "xs:unsignedInt": ("unsigned int", 4, "DEC"),
    "xs:long": ("bsls::Types::Int64", 8, "DEC"),
    "xs:unsignedLong": ("bsls::Types::Uint64", 8, "DEC"),
    "xs:double": ("double", 8, "DEFAULT"),
    "xs:boolean": ("bool", 1, "TEXT"),
    "xs:hexBinary": ("bsl::vector<char>", 24, "HEX"),
    "xs:base64Binary": ("bsl::vector<char>", 24, "BASE64"),
    "xs:decimal": ("double", 8, "DEC"),
    "xs:nonNegativeInteger": ("bsls::Types::Uint64", 8, "DEC"),
    "xs:date": ("bdlt::DateTz", 8, "DEFAULT"),
    "xs:time": ("bdlt::TimeTz", 8, "DEFAULT"),
    "xs:dateTime": ("bdlt::DatetimeTz", 16, "DEFAULT"),
}


@dataclass
class EnumValue:
    name: str
    id: int


@dataclass
class EnumType:
    name: str
    doc: str
    values: list[EnumValue] = field(default_factory=list)


@dataclass
class Field:
    name: str
    type_name: str  # XSD type reference (e.g. "xs:string" or "tns:Foo")
    kind: FieldKind = FieldKind.REGULAR
    default: str | None = None
    doc: str = ""
    id: int | None = None  # bdem:id from XSD; drives ATTRIBUTE_ID/SELECTION_ID


@dataclass
class ComplexType:
    name: str
    kind: TypeKind
    doc: str = ""
    # For SEQUENCE and HYBRID: the regular sequence fields
    fields: list[Field] = field(default_factory=list)
    # For CHOICE and HYBRID: the choice alternatives
    choices: list[Field] = field(default_factory=list)
    # For HYBRID: position of <choice> among sibling <element>s in the XSD
    choice_index: int = 0


@dataclass
class Schema:
    """Top-level container for all types in one XSD file."""

    enums: list[EnumType] = field(default_factory=list)
    types: list[ComplexType] = field(default_factory=list)

    def type_by_name(self, name: str) -> ComplexType | EnumType | None:
        for e in self.enums:
            if e.name == name:
                return e
        for t in self.types:
            if t.name == name:
                return t
        return None
