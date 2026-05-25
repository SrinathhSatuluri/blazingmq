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

"""BDE naming convention transformations."""

from __future__ import annotations

import re


def _hyphen_to_camel(name: str) -> str:
    """Convert hyphenated-name to camelCase: 'batch-post' -> 'batchPost'."""
    if "-" not in name:
        return name
    parts = name.split("-")
    return parts[0] + "".join(p.capitalize() for p in parts[1:])


def to_upper_snake(name: str) -> str:
    """Convert camelCase/PascalCase to UPPER_SNAKE_CASE per BDE rules.

    Rule: insert '_' before every uppercase letter, then uppercase everything.
    Hyphens are treated as word separators.
    Examples:
        'asJSON'         -> 'AS_J_S_O_N'
        'help'           -> 'HELP'
        'configProvider' -> 'CONFIG_PROVIDER'
        'TEXT'           -> 'TEXT'
        'batch-post'     -> 'BATCH_POST'
    """
    name = _hyphen_to_camel(name)
    result = re.sub(r"([A-Z])", r"_\1", name)
    return result.strip("_").upper()


def to_member_name(name: str) -> str:
    """Convert element name to d_memberName (camelCase with 'd_' prefix).

    BDE convention: the first character is always lowercase.
    XSD elements like 'Routing' become 'd_routing'.
    Hyphens are converted to camelCase: 'batch-post' -> 'd_batchPost'.
    """
    name = _hyphen_to_camel(name)
    return f"d_{name[0].lower()}{name[1:]}" if name else "d_"


def to_accessor_name(name: str) -> str:
    """Element name to accessor (camelCase, first char lowered).

    XSD elements like 'Routing' become accessor 'routing()'.
    Hyphens are converted to camelCase: 'batch-post' -> 'batchPost'.
    """
    name = _hyphen_to_camel(name)
    return f"{name[0].lower()}{name[1:]}" if name else name


def to_class_name(xsd_name: str) -> str:
    """XSD type name maps directly to C++ class name (PascalCase already)."""
    return xsd_name


def to_enum_value(value_name: str) -> str:
    """Enum values are uppercased from XSD.

    Examples:
        'TEXT'         -> 'TEXT'
        'JSON_COMPACT' -> 'JSON_COMPACT'
        'remote'       -> 'REMOTE'
        'alive'        -> 'ALIVE'
    """
    return value_name.upper()


def to_title_name(name: str) -> str:
    """Convert element name to TitleCase for make<Name> methods.

    'batch-post' -> 'BatchPost', 'confirm' -> 'Confirm'.
    """
    name = _hyphen_to_camel(name)
    return f"{name[0].upper()}{name[1:]}" if name else name


def to_manipulator_name(name: str) -> str:
    """Element name to manipulator (same as accessor — camelCase).

    Hyphens are converted to camelCase: 'batch-post' -> 'batchPost'.
    """
    return _hyphen_to_camel(name)


def component_name(package: str, component: str) -> str:
    """Build the BDE component name: 'package_component'."""
    return f"{package}_{component}"


def make_banner(component_full: str, ext: str) -> str:
    """Build the *DO NOT EDIT* banner line, padded to 79 chars.

    Format: ``// {component}.{ext}  ...  *DO NOT EDIT*  ...  @generated -*-C++-*-``
    The spaces on each side of ``*DO NOT EDIT*`` are equal.
    """
    prefix = f"// {component_full}.{ext}"
    suffix = "@generated -*-C++-*-"
    marker = "*DO NOT EDIT*"
    # Total line is 79 chars
    # Layout: prefix + pad + marker + pad + suffix
    inner = 79 - len(prefix) - len(suffix)
    # marker sits in the middle of the inner space
    left_pad = (inner - len(marker)) // 2
    right_pad = inner - len(marker) - left_pad
    return f"{prefix}{' ' * left_pad}{marker}{' ' * right_pad}{suffix}"
