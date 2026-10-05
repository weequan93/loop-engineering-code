"""Restricted generation schemas; the original contract still validates results.

Codex and OpenAI Structured Outputs require explicit scalar types and closed,
fully required objects. Local-only length/uniqueness constraints remain enforced
by the controller after a response, rather than being sent as unsupported keys.
"""

from copy import deepcopy

from .contracts import ContractError


def structured_output_schema(schema):
    result = deepcopy(schema)
    allowed = {"type", "description", "properties", "required", "additionalProperties",
               "items", "enum", "anyOf", "$defs", "$ref", "pattern", "format",
               "minItems", "maxItems", "multipleOf", "minimum", "maximum",
               "exclusiveMinimum", "exclusiveMaximum"}

    def scalar_type(value):
        if value is None:
            return "null"
        if type(value) is bool:
            return "boolean"
        if type(value) is str:
            return "string"
        if type(value) is int:
            return "integer"
        if type(value) is float:
            return "number"
        raise ContractError("Structured output constants/enums must be scalar")

    def visit(node):
        if type(node) is not dict:
            raise ContractError("Structured output schema must contain object schemas")
        for key in ("$schema", "$id", "title", "minLength", "maxLength", "uniqueItems"):
            node.pop(key, None)
        if "const" in node:
            value = node.pop("const")
            if "enum" in node and value not in node["enum"]:
                raise ContractError("Structured output constant conflicts with enum")
            node["enum"] = [value]
        if "type" not in node and "enum" in node:
            kinds = list(dict.fromkeys(scalar_type(value) for value in node["enum"]))
            if not kinds:
                raise ContractError("Structured output enum must be nonempty")
            node["type"] = kinds[0] if len(kinds) == 1 else kinds
        if "type" not in node and not ({"anyOf", "$ref"} & node.keys()):
            raise ContractError("Structured output schema lacks a type")
        if set(node) - allowed:
            raise ContractError("Unsupported structured output schema keys: " + ", ".join(sorted(set(node) - allowed)))
        if node.get("type") == "object":
            properties = node.get("properties", {})
            required = node.get("required", [])
            if (type(properties) is not dict or type(required) is not list
                    or set(required) != set(properties) or len(required) != len(properties)
                    or node.get("additionalProperties") is not False):
                raise ContractError("Structured output objects must be closed with every field required")
            for child in properties.values():
                visit(child)
        if "items" in node:
            visit(node["items"])
        for child in node.get("anyOf", []):
            visit(child)
        for child in node.get("$defs", {}).values():
            visit(child)

    visit(result)
    if result.get("type") != "object" or "anyOf" in result:
        raise ContractError("Structured output root must be an object without anyOf")
    return result
