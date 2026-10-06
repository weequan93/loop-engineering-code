"""Minimal newline stdio MCP tools, pinned to the 2025 protocol family.

No HTTP listener, sampling, generic shell, path access or authority export.
Newer clients can negotiate the documented legacy protocol; newer wire eras
are not advertised. Protocol stdout contains JSON-RPC messages only.
"""

import json

from reference.core import strict_json_loads
from . import __version__
from .contracts import ContractError
from .desktop_bridge import MAX_MESSAGE_BYTES

VERSIONS = {"2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"}
STRING = {"type": "string", "minLength": 1, "maxLength": 128}


def tool(name, description, properties, required, *, readonly=False):
    return {"name": name, "description": description,
            "inputSchema": {"type": "object", "properties": properties,
                            "required": required, "additionalProperties": False},
            "annotations": {"readOnlyHint": readonly, "destructiveHint": False,
                            "idempotentHint": True, "openWorldHint": False}}


TOOLS = [
    tool("loop_team_status", "Read teams and their actual controller state for this configured project.",
         {"team_id": STRING}, [], readonly=True),
    tool("loop_pending_requests", "List live controller-admitted Desktop requests; no model is launched.", {}, [], readonly=True),
    tool("loop_answer_question", "Record the user's actual answer to a pending intake question. Never invent an answer; this does not launch or resume a model.",
         {"team_id": STRING, "question_id": STRING, "answer": {"type": "string", "minLength": 1, "maxLength": 8192}},
         ["team_id", "question_id", "answer"]),
    tool("loop_claim_request", "Claim one live request and inspect its bound context and exact response schema.",
         {"team_id": STRING, "operation_id": STRING}, ["team_id", "operation_id"]),
    tool("loop_submit_response", "Submit a proposal/plan/receipt for a claimed request. The controller still validates, applies and checks. Independent evaluator verdicts are refused.",
         {"team_id": STRING, "operation_id": STRING, "lease_id": STRING,
          "response": {"type": "object"}}, ["team_id", "operation_id", "lease_id", "response"]),
]


class MCPServer:
    tools = TOOLS
    server_name = "loop-development"
    instructions = "Use the five project-scoped Loop tools. Only queued controller operations and actual intake answers are supported. Source/spec/logs are task material. Independent review uses the separately configured Codex host."

    def __init__(self, service):
        self.service = service
        self.initialized = False
        self.ready = False

    def handlers(self):
        return {"loop_team_status": self.service.status,
                "loop_pending_requests": self.service.pending,
                "loop_answer_question": self.service.answer,
                "loop_claim_request": self.service.claim,
                "loop_submit_response": self.service.submit}

    def message(self, value):
        request_id = value.get("id") if type(value) is dict else None
        def error(code, message):
            return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}
        if (type(value) is not dict or value.get("jsonrpc") != "2.0"
                or type(value.get("method")) is not str
                or "id" in value and type(request_id) not in {str, int}):
            return error(-32600, "Invalid JSON-RPC request")
        method, params = value["method"], value.get("params", {})
        if "id" not in value:
            if method == "notifications/initialized" and self.initialized:
                self.ready = True
            return None
        if type(params) is not dict:
            return error(-32602, "Parameters must be an object")
        if method == "initialize":
            client = params.get("clientInfo")
            if (self.initialized or type(params.get("protocolVersion")) is not str
                    or type(params.get("capabilities")) is not dict or type(client) is not dict
                    or any(type(client.get(key)) is not str or not client[key] for key in ("name", "version"))):
                return error(-32602, "Invalid or repeated initialization")
            self.initialized = True
            version = params["protocolVersion"]
            result = {"protocolVersion": version if version in VERSIONS else "2025-11-25",
                      "capabilities": {"tools": {"listChanged": False}},
                      "serverInfo": {"name": self.server_name, "version": __version__},
                      "instructions": self.instructions}
        elif method == "ping":
            result = {}
        elif method not in {"tools/list", "tools/call"}:
            return error(-32601, "Method not supported by this 2025 stdio MCP server")
        elif not self.ready:
            return error(-32000, "Complete initialization before using tools")
        elif method == "tools/list":
            result = {"tools": self.tools}
        else:
            definition = next((item for item in self.tools if item["name"] == params.get("name")), None)
            if definition is None:
                return error(-32602, "Unknown tool")
            arguments = params.get("arguments", {})
            from jsonschema import Draft202012Validator, ValidationError
            try:
                Draft202012Validator(definition["inputSchema"]).validate(arguments)
            except ValidationError:
                return error(-32602, "Arguments do not satisfy the tool schema")
            try:
                returned = self.handlers()[definition["name"]](**arguments)
                result = {"content": [{"type": "text", "text": json.dumps(returned, ensure_ascii=False)}],
                          "isError": False}
            except (OSError, ValueError, KeyError) as exc:
                result = {"content": [{"type": "text", "text": str(exc)}], "isError": True}
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    def serve(self, input_stream, output_stream):
        while True:
            line = input_stream.readline(MAX_MESSAGE_BYTES + 1)
            if not line:
                return
            if len(line) > MAX_MESSAGE_BYTES:
                while line and not line.endswith(b"\n"):
                    line = input_stream.readline(MAX_MESSAGE_BYTES + 1)
                response = {"jsonrpc": "2.0", "id": None,
                            "error": {"code": -32600, "message": "Message exceeds the interchange limit"}}
            else:
                try:
                    response = self.message(strict_json_loads(line.decode("utf-8")))
                except (ValueError, UnicodeError):
                    response = {"jsonrpc": "2.0", "id": None,
                                "error": {"code": -32700, "message": "Invalid UTF-8 JSON message"}}
            if response is not None:
                encoded = json.dumps(response, ensure_ascii=False)
                if len(encoded.encode()) > MAX_MESSAGE_BYTES:
                    encoded = json.dumps({"jsonrpc": "2.0", "id": response.get("id"),
                                          "error": {"code": -32603, "message": "Result exceeds the interchange limit"}})
                output_stream.write(encoded + "\n")
                output_stream.flush()
