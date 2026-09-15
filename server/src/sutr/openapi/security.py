"""Translate OpenAPI security schemes into the runtime's credential model.

Build prompt §24 sets the bar: API key in header, query, and cookie; Bearer;
Basic; OAuth2 authorization code; OAuth2 client credentials; OpenID Connect
where practical; mTLS; more than one scheme at a time — and, explicitly,
*"do not silently degrade OAuth2 into pasted bearer tokens"*.

The model is a list of `CredentialPlacement`s rather than the single
header/format pair the platform started with. Each placement says where a
credential goes on the wire and what has to be supplied to obtain it:

- `apiKey`   → a stored secret, placed in a header, query parameter, or cookie
- `http`     → bearer or basic, always a header
- `oauth2`   → the *grant* is recorded, not flattened: client-credentials is
               executed by the platform (it owns the token endpoint call and
               the refresh), authorization-code reuses connected accounts
- `mutualTLS`→ recorded and reported as unsupported, because a client
               certificate is not a value that can be pasted into a header
               and pretending otherwise would produce a tool that always 401s

`AuthTranslation.token_header` / `.token_format` are kept as derived
properties of the first header placement, so every existing caller, stored
integration, and test keeps working unchanged (ADR-009).
"""

from pydantic import BaseModel

from sutr.openapi.errors import SpecWarning
from sutr.openapi.normalizer import ApiDefinition, SecurityScheme

# What the platform must be given before a placement can be used.
NEEDS_SECRET = "secret"  # a single opaque value (API key, bearer token)
NEEDS_BASIC = "basic"  # a username:password pair, base64-encoded
NEEDS_CLIENT_CREDENTIALS = "client_credentials"  # client id + secret + token URL
NEEDS_AUTHORIZATION_CODE = "authorization_code"  # an OAuth grant via connected accounts
NEEDS_NOTHING = "none"
UNSUPPORTED = "unsupported"


class OAuthFlow(BaseModel):
    """One OAuth2 grant declared by the specification, kept intact."""

    kind: str  # clientCredentials | authorizationCode | implicit | password
    token_url: str | None = None
    authorization_url: str | None = None
    refresh_url: str | None = None
    scopes: list[str] = []


class CredentialPlacement(BaseModel):
    """One credential, and where it goes on the wire."""

    scheme_name: str
    scheme_type: str  # apiKey | http | oauth2 | openIdConnect | mutualTLS
    location: str = "header"  # header | query | cookie
    name: str = ""  # header name, query key, or cookie name
    format: str = "{token}"  # carries the literal {token} slot
    requires: str = NEEDS_SECRET
    credential_hint: str = ""
    # OAuth2 / OpenID Connect only.
    flow: OAuthFlow | None = None
    openid_connect_url: str | None = None

    @property
    def usable(self) -> bool:
        return self.requires != UNSUPPORTED


class AuthTranslation(BaseModel):
    # Derived from the first usable header placement — the shape the platform
    # has always stored and the one every hand-built integration uses.
    token_header: str
    token_format: str
    scheme_name: str | None = None  # which spec scheme was chosen (None → no auth)
    # What the user should paste as the "token" (guidance for the UI).
    credential_hint: str | None = None
    warnings: list[SpecWarning] = []
    # Every scheme the specification declares that the runtime can act on,
    # including query/cookie API keys and OAuth2 grants.
    placements: list[CredentialPlacement] = []
    # Schemes required together (an AND requirement in the spec's `security`).
    # Names, in the order the specification listed them.
    required_together: list[str] = []

    @property
    def usable_placements(self) -> list[CredentialPlacement]:
        return [p for p in self.placements if p.usable]


_NO_AUTH = AuthTranslation(token_header="", token_format="", scheme_name=None)


def _oauth_flows(scheme: SecurityScheme) -> list[OAuthFlow]:
    flows = []
    for kind, flow in (scheme.flows or {}).items():
        if not isinstance(flow, dict):
            continue
        flows.append(
            OAuthFlow(
                kind=str(kind),
                token_url=flow.get("tokenUrl"),
                authorization_url=flow.get("authorizationUrl"),
                refresh_url=flow.get("refreshUrl"),
                scopes=sorted((flow.get("scopes") or {}).keys()),
            )
        )
    # Client credentials first: it is the only grant the platform can complete
    # without a human, so it is the one to prefer for an agent-facing tool.
    order = {"clientCredentials": 0, "authorizationCode": 1, "password": 2, "implicit": 3}
    flows.sort(key=lambda f: order.get(f.kind, 9))
    return flows


def _placement_for(scheme: SecurityScheme) -> CredentialPlacement:
    """Map one scheme onto a credential placement.

    Every scheme produces a placement — including ones the runtime cannot use,
    which are marked UNSUPPORTED with a reason. Nothing is dropped silently.
    """
    if scheme.type == "apiKey":
        location = scheme.location or "header"
        if location not in ("header", "query", "cookie") or not scheme.param_name:
            return CredentialPlacement(
                scheme_name=scheme.name,
                scheme_type=scheme.type,
                requires=UNSUPPORTED,
                credential_hint=(
                    f"The apiKey scheme '{scheme.name}' declares no usable name/location."
                ),
            )
        placement_label = {
            "header": f"the '{scheme.param_name}' header",
            "query": f"the '{scheme.param_name}' query parameter",
            "cookie": f"the '{scheme.param_name}' cookie",
        }[location]
        return CredentialPlacement(
            scheme_name=scheme.name,
            scheme_type=scheme.type,
            location=location,
            name=scheme.param_name,
            format="{token}",
            requires=NEEDS_SECRET,
            credential_hint=f"API key sent in {placement_label}.",
        )

    if scheme.type == "http":
        http_scheme = (scheme.scheme or "").lower()
        if http_scheme == "bearer":
            return CredentialPlacement(
                scheme_name=scheme.name,
                scheme_type=scheme.type,
                location="header",
                name="Authorization",
                format="Bearer {token}",
                requires=NEEDS_SECRET,
                credential_hint="Bearer token sent in the Authorization header.",
            )
        if http_scheme == "basic":
            return CredentialPlacement(
                scheme_name=scheme.name,
                scheme_type=scheme.type,
                location="header",
                name="Authorization",
                format="Basic {token}",
                requires=NEEDS_BASIC,
                credential_hint="Username and password, sent base64-encoded as HTTP Basic.",
            )
        return CredentialPlacement(
            scheme_name=scheme.name,
            scheme_type=scheme.type,
            requires=UNSUPPORTED,
            credential_hint=(
                f"HTTP authentication scheme '{scheme.scheme}' is not one the runtime can "
                "construct (only bearer and basic are)."
            ),
        )

    if scheme.type in ("oauth2", "openIdConnect"):
        flows = _oauth_flows(scheme)
        chosen = flows[0] if flows else None
        if chosen is None:
            # OpenID Connect declares its endpoints in a discovery document
            # rather than inline, so an absent flow list is normal there.
            if scheme.type == "openIdConnect" and scheme.openid_connect_url:
                return CredentialPlacement(
                    scheme_name=scheme.name,
                    scheme_type=scheme.type,
                    location="header",
                    name="Authorization",
                    format="Bearer {token}",
                    requires=NEEDS_AUTHORIZATION_CODE,
                    openid_connect_url=scheme.openid_connect_url,
                    credential_hint=(
                        "OpenID Connect: endpoints are discovered from "
                        f"{scheme.openid_connect_url}."
                    ),
                )
            return CredentialPlacement(
                scheme_name=scheme.name,
                scheme_type=scheme.type,
                requires=UNSUPPORTED,
                credential_hint=(
                    f"The {scheme.type} scheme '{scheme.name}' declares no usable flow, so no "
                    "grant can be run for it."
                ),
            )
        if chosen.kind == "clientCredentials":
            requires = NEEDS_CLIENT_CREDENTIALS
            hint = (
                "OAuth2 client credentials: supply a client id and secret; the platform calls "
                f"{chosen.token_url} and refreshes the token itself."
            )
        elif chosen.kind == "authorizationCode":
            requires = NEEDS_AUTHORIZATION_CODE
            hint = (
                "OAuth2 authorization code: authorize once through a connected account; the "
                "platform holds and refreshes the token."
            )
        else:
            # implicit and password grants: implicit issues tokens to browsers
            # and is deprecated; password requires end-user credentials the
            # platform must not hold.
            requires = UNSUPPORTED
            hint = (
                f"OAuth2 '{chosen.kind}' grant is not run by the platform. Supply an access "
                "token obtained out of band instead."
            )
        return CredentialPlacement(
            scheme_name=scheme.name,
            scheme_type=scheme.type,
            location="header",
            name="Authorization",
            format="Bearer {token}",
            requires=requires,
            flow=chosen,
            openid_connect_url=scheme.openid_connect_url,
            credential_hint=hint,
        )

    if scheme.type == "mutualTLS":
        return CredentialPlacement(
            scheme_name=scheme.name,
            scheme_type=scheme.type,
            requires=UNSUPPORTED,
            credential_hint=(
                "Mutual TLS requires a client certificate presented during the TLS handshake, "
                "which the hosted runtime cannot do. Deploy a standalone generated server with "
                "the certificate mounted, or use another scheme the API offers."
            ),
        )

    return CredentialPlacement(
        scheme_name=scheme.name,
        scheme_type=scheme.type,
        requires=UNSUPPORTED,
        credential_hint=f"Unknown security scheme type '{scheme.type}'.",
    )


def translate_security(definition: ApiDefinition) -> AuthTranslation:
    """Translate every declared scheme, and pick a primary header credential.

    Preference order for the primary: a scheme named in the global security
    requirement, then any other scheme, in declaration order. Schemes that
    cannot be used are still returned — as placements marked unsupported and
    as warnings — so the reason is visible rather than inferred from silence.
    """
    warnings: list[SpecWarning] = []
    schemes_by_name = {s.name: s for s in definition.security_schemes}

    ordered: list[SecurityScheme] = []
    for name in definition.global_security:
        if name in schemes_by_name and schemes_by_name[name] not in ordered:
            ordered.append(schemes_by_name[name])
    for scheme in definition.security_schemes:
        if scheme not in ordered:
            ordered.append(scheme)

    placements = [_placement_for(scheme) for scheme in ordered]
    for placement in placements:
        if not placement.usable:
            warnings.append(
                SpecWarning(
                    code="unsupported_security_scheme",
                    message=(
                        f"Security scheme '{placement.scheme_name}' "
                        f"({placement.scheme_type}) cannot be used: {placement.credential_hint}"
                    ),
                    context=placement.scheme_name,
                )
            )

    usable = [p for p in placements if p.usable]
    # The primary is the first usable *header* credential, because that is what
    # the single-credential storage model can hold. A query or cookie API key
    # is still returned as a placement and is applied by the runtime.
    primary = next((p for p in usable if p.location == "header"), None)
    if primary is None and usable:
        primary = usable[0]
        warnings.append(
            SpecWarning(
                code="non_header_primary_credential",
                message=(
                    f"The API's credential is sent in the {primary.location} "
                    f"('{primary.name}'), not a header."
                ),
                context=primary.scheme_name,
            )
        )

    if primary is None:
        result = _NO_AUTH.model_copy(deep=True)
        result.warnings = warnings
        result.placements = placements
        result.required_together = list(definition.global_security)
        return result

    return AuthTranslation(
        token_header=primary.name if primary.location == "header" else "",
        token_format=primary.format if primary.location == "header" else "",
        scheme_name=primary.scheme_name,
        credential_hint=primary.credential_hint,
        warnings=warnings,
        placements=placements,
        required_together=list(definition.global_security),
    )
