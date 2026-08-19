"""OpenAPI → MCP tool compiler.

Pipeline:  raw text → parse (loader) → validate + $ref-resolve + normalize
(normalizer, resolver) → IR → filter + compile to ApiTool records (compiler)
with security translation (security). Compiled tools are stored on a
CustomApiIntegration row, so discovery, approvals, execution, logging, and the
MCP gateway all apply unchanged — this is the platform's dynamic execution
mode for imported APIs.
"""

from sutr.openapi.compiler import (
    CompileFilters,
    CompileResult,
    compile_definition,
)
from sutr.openapi.errors import OpenAPIError, SpecWarning
from sutr.openapi.loader import fetch_spec_from_url, parse_spec_text
from sutr.openapi.normalizer import ApiDefinition, normalize, substitute_server_url
from sutr.openapi.security import AuthTranslation, translate_security

__all__ = [
    "ApiDefinition",
    "AuthTranslation",
    "CompileFilters",
    "CompileResult",
    "OpenAPIError",
    "SpecWarning",
    "compile_definition",
    "fetch_spec_from_url",
    "normalize",
    "parse_spec_text",
    "substitute_server_url",
    "translate_security",
]
