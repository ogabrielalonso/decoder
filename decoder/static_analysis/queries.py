"""Tree-sitter queries per language for symbols and imports.

Capture naming convention:
    <kind>.name   -> the identifier node whose text is the symbol name
    <kind>.body   -> the full definition node (used for line span)

<kind> values map 1:1 to SymbolKind in decoder.schemas.
"""

from __future__ import annotations

SYMBOL_QUERIES: dict[str, str] = {
    "python": """
(function_definition name: (identifier) @function.name) @function.body
(class_definition name: (identifier) @class.name) @class.body
""",
    "javascript": """
(function_declaration name: (identifier) @function.name) @function.body
(class_declaration name: (identifier) @class.name) @class.body
(method_definition name: (property_identifier) @method.name) @method.body
(variable_declarator
  name: (identifier) @function.name
  value: [(arrow_function) (function_expression)]) @function.body
""",
    "typescript": """
(function_declaration name: (identifier) @function.name) @function.body
(class_declaration name: (type_identifier) @class.name) @class.body
(interface_declaration name: (type_identifier) @interface.name) @interface.body
(type_alias_declaration name: (type_identifier) @type.name) @type.body
(method_definition name: (property_identifier) @method.name) @method.body
(variable_declarator
  name: (identifier) @function.name
  value: [(arrow_function) (function_expression)]) @function.body
""",
    "tsx": """
(function_declaration name: (identifier) @function.name) @function.body
(class_declaration name: (type_identifier) @class.name) @class.body
(interface_declaration name: (type_identifier) @interface.name) @interface.body
(type_alias_declaration name: (type_identifier) @type.name) @type.body
(method_definition name: (property_identifier) @method.name) @method.body
""",
    "go": """
(function_declaration name: (identifier) @function.name) @function.body
(method_declaration name: (field_identifier) @method.name) @method.body
(type_declaration
  (type_spec name: (type_identifier) @type.name
             type: (struct_type))) @struct.body
(type_declaration
  (type_spec name: (type_identifier) @type.name
             type: (interface_type))) @interface.body
(type_declaration
  (type_spec name: (type_identifier) @type.name)) @type.body
""",
    "rust": """
(function_item name: (identifier) @function.name) @function.body
(struct_item name: (type_identifier) @struct.name) @struct.body
(enum_item name: (type_identifier) @enum.name) @enum.body
(trait_item name: (type_identifier) @trait.name) @trait.body
""",
    "java": """
(method_declaration name: (identifier) @function.name) @function.body
(class_declaration name: (identifier) @class.name) @class.body
(interface_declaration name: (identifier) @interface.name) @interface.body
(enum_declaration name: (identifier) @enum.name) @enum.body
""",
    "kotlin": """
(function_declaration (simple_identifier) @function.name) @function.body
(class_declaration (type_identifier) @class.name) @class.body
(object_declaration (type_identifier) @class.name) @class.body
""",
}


IMPORT_QUERIES: dict[str, str] = {
    "python": """
(import_statement name: (dotted_name) @import.module) @import.node
(import_from_statement module_name: (dotted_name) @from_import.module) @from_import.node
(import_from_statement module_name: (relative_import) @from_import.module) @from_import.node
""",
    "javascript": """
(import_statement source: (string (string_fragment) @import.module)) @import.node
""",
    "typescript": """
(import_statement source: (string (string_fragment) @import.module)) @import.node
""",
    "tsx": """
(import_statement source: (string (string_fragment) @import.module)) @import.node
""",
    "go": """
(import_spec path: (interpreted_string_literal) @import.module) @import.node
""",
    "rust": """
(use_declaration) @use.node
""",
    "java": """
(import_declaration (scoped_identifier) @import.module) @import.node
""",
    "kotlin": """
(import_header (identifier) @import.module) @import.node
""",
}


# Capture prefix -> SymbolKind
KIND_MAP: dict[str, str] = {
    "function": "function",
    "method": "method",
    "class": "class",
    "struct": "struct",
    "enum": "enum",
    "interface": "interface",
    "trait": "trait",
    "type": "type",
    "variable": "variable",
}
