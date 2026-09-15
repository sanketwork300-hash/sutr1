"""Which connectors exist, and which are honestly declared but not built.

Two lists on purpose. `CONNECTORS` is what works. `PLANNED` names the source
types the LLD asks for that are **not implemented**, each with the reason —
because a source type that is silently absent looks like an oversight, while
one that is listed as unavailable with a reason is a decision somebody made.
"""

from dataclasses import dataclass

from sutr.source_connectors.base import SourceConnector
from sutr.source_connectors.git import GitHubConnector, SwaggerHubConnector
from sutr.source_connectors.inline import PasteConnector, UploadConnector
from sutr.source_connectors.postman import PostmanConnector
from sutr.source_connectors.url import SwaggerUiConnector, UrlConnector

_CONNECTORS: dict[str, SourceConnector] = {
    connector.id: connector
    for connector in (
        PasteConnector(),
        UploadConnector(),
        UrlConnector(),
        SwaggerUiConnector(),
        GitHubConnector(),
        SwaggerHubConnector(),
        PostmanConnector(),
    )
}


@dataclass(frozen=True)
class PlannedConnector:
    """A source type the LLD names that is not implemented, and why."""

    id: str
    display_name: str
    reason: str


PLANNED: tuple[PlannedConnector, ...] = (
    PlannedConnector(
        id="wsdl",
        display_name="WSDL / SOAP",
        reason=(
            "Not implemented. Converting WSDL to OpenAPI means mapping XML Schema types, "
            "SOAP envelopes and operation styles onto a JSON contract — a translation with "
            "enough judgement in it to be wrong quietly. The LLD's stack names Zeep for the "
            "parsing; the mapping decisions still need to be made and agreed."
        ),
    ),
    PlannedConnector(
        id="api_gateway",
        display_name="API gateway",
        reason=(
            "Not implemented, and BLOCKED: the LLD says 'API gateways' without naming which. "
            "Apigee, Kong, AWS API Gateway and Azure APIM have entirely different APIs and "
            "auth models, so building one is a guess about which was meant. Tracked as open "
            "item B-3."
        ),
    ),
    PlannedConnector(
        id="generic_git",
        display_name="GitLab / Bitbucket / generic Git",
        reason=(
            "Not implemented. The GitHub connector is real; the others need their own API "
            "clients and their own connected-account flows rather than a shared 'git' path."
        ),
    ),
)


def get(connector_id: str) -> SourceConnector | None:
    return _CONNECTORS.get(connector_id)


def require(connector_id: str) -> SourceConnector:
    connector = get(connector_id)
    if connector is None:
        from sutr.source_connectors.base import ConnectorError

        available = ", ".join(sorted(_CONNECTORS))
        raise ConnectorError(
            "unknown_connector",
            f"No source connector '{connector_id}'. Available: {available}.",
        )
    return connector


def all_connectors() -> list[SourceConnector]:
    return [_CONNECTORS[key] for key in sorted(_CONNECTORS)]


def describe_all() -> dict:
    return {
        "connectors": [connector.describe() for connector in all_connectors()],
        "planned": [
            {
                "id": planned.id,
                "display_name": planned.display_name,
                "available": False,
                "unavailable_reason": planned.reason,
            }
            for planned in PLANNED
        ],
    }
