"""OpenAPI → MCP tool compiler.

Pipeline:  raw text → parse (loader) → validate + $ref-resolve + normalize
(normalizer, resolver) → IR → filter + compile to ApiTool records (compiler)
with security translation (security). Compiled tools are stored on a
CustomApiIntegration row, so discovery, approvals, execution, logging, and the
MCP gateway all apply unchanged — this is the platform's dynamic execution
mode for imported APIs.
"""

from agent_port.openapi.compiler import (
    CompileFilters,
    CompileResult,
    compile_definition,
)
from agent_port.openapi.errors import OpenAPIError, Warning_
from agent_port.openapi.loader import fetch_spec_from_url, parse_spec_text
from agent_port.openapi.normalizer import ApiDefinition, normalize, substitute_server_url
from agent_port.openapi.security import AuthTranslation, translate_security

__all__ = [
    "ApiDefinition",
    "AuthTranslation",
    "CompileFilters",
    "CompileResult",
    "OpenAPIError",
    "Warning_",
    "compile_definition",
    "fetch_spec_from_url",
    "normalize",
    "parse_spec_text",
    "substitute_server_url",
    "translate_security",
]
