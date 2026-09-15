"""OpenAPI lint rules and ad-hoc linting.

Separate from `openapi_projects.py` because it is a different resource: rules
are static platform metadata, and linting a document is a pure function that
stores nothing. Keeping it here means a caller can check a specification
*before* deciding to import it.
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from sutr.dependencies import AgentAuth, get_agent_auth
from sutr.openapi.errors import OpenAPIError
from sutr.openapi.linting import all_rules, lint_document
from sutr.openapi.linting.schema_errors import schema_findings
from sutr.openapi.loader import parse_spec_text

# Registered before the project router so "lint" is never parsed as a project id.
router = APIRouter(prefix="/api/openapi/lint", tags=["openapi"])


class LintRequest(BaseModel):
    content: str


@router.get("/rules")
def list_rules(_: AgentAuth = Depends(get_agent_auth)) -> list[dict]:
    """Every rule the linter can report, with its severity and summary."""
    return [
        {
            "rule_id": entry.id,
            "severity": entry.severity.value,
            "summary": entry.summary,
            "versions": list(entry.versions),
            "documentation": f"/openapi-linting#{entry.id}",
        }
        for entry in all_rules()
    ]


@router.post("")
def lint(body: LintRequest, _: AgentAuth = Depends(get_agent_auth)) -> dict:
    """Lint a specification without importing it.

    Structural violations and rule findings are returned together, because to
    the person fixing the document they are one list of things to fix. A
    document that fails structural validation is still linted where the rules
    can run, so one bad `responses` block does not hide six naming problems.
    """
    try:
        document = parse_spec_text(body.content)
    except OpenAPIError as exc:
        raise HTTPException(status_code=400, detail={"error": exc.code, "message": exc.message})

    version = str(document.get("openapi") or document.get("swagger") or "")
    if not version:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "missing_version",
                "message": "The document does not declare an 'openapi' version field.",
            },
        )

    findings = list(schema_findings(document, version)) + lint_document(document).findings
    counts = {"error": 0, "warning": 0, "info": 0}
    for finding in findings:
        counts[finding.severity.value.lower()] += 1
    return {
        "openapi_version": version,
        "counts": counts,
        "findings": [f.model_dump(mode="json") for f in findings],
    }
