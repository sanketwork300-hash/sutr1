"""Translate OpenAPI security schemes into the runtime's credential model.

The execution runtime injects exactly one credential as an HTTP header
(`token_header` + `token_format` with a literal `{token}` slot, see
`token_auth.build_token_auth_headers`). This module picks the best-supported
scheme from the spec and maps it onto that model, surfacing warnings for
anything that cannot be represented instead of silently dropping it.
"""

from pydantic import BaseModel

from sutr.openapi.errors import SpecWarning
from sutr.openapi.normalizer import ApiDefinition, SecurityScheme


class AuthTranslation(BaseModel):
    token_header: str
    token_format: str
    scheme_name: str | None = None  # which spec scheme was chosen (None → no auth)
    # What the user should paste as the "token" (guidance for the UI).
    credential_hint: str | None = None
    warnings: list[SpecWarning] = []


_NO_AUTH = AuthTranslation(token_header="", token_format="", scheme_name=None)


def _translate_scheme(scheme: SecurityScheme) -> AuthTranslation | None:
    if scheme.type == "apiKey":
        if scheme.location == "header" and scheme.param_name:
            return AuthTranslation(
                token_header=scheme.param_name,
                token_format="{token}",
                scheme_name=scheme.name,
                credential_hint=f"API key sent as the '{scheme.param_name}' header.",
            )
        return None  # query/cookie placement handled by caller with a warning
    if scheme.type == "http":
        http_scheme = (scheme.scheme or "").lower()
        if http_scheme == "bearer":
            return AuthTranslation(
                token_header="Authorization",
                token_format="Bearer {token}",
                scheme_name=scheme.name,
                credential_hint="Bearer token sent in the Authorization header.",
            )
        if http_scheme == "basic":
            return AuthTranslation(
                token_header="Authorization",
                token_format="Basic {token}",
                scheme_name=scheme.name,
                credential_hint="Base64-encoded 'username:password' pair.",
            )
        return None
    if scheme.type in ("oauth2", "openIdConnect"):
        # The platform doesn't run the provider's OAuth flow for imported APIs;
        # the user supplies an access token obtained out of band.
        return AuthTranslation(
            token_header="Authorization",
            token_format="Bearer {token}",
            scheme_name=scheme.name,
            credential_hint=(
                "OAuth2-protected API: paste an access token obtained from the provider."
            ),
        )
    return None


def translate_security(definition: ApiDefinition) -> AuthTranslation:
    """Pick a runtime auth config from the spec's schemes.

    Preference order: a scheme named in the global security requirement, then
    any translatable scheme, then no auth. Untranslatable schemes produce
    warnings rather than being silently ignored.
    """
    warnings: list[SpecWarning] = []
    schemes_by_name = {s.name: s for s in definition.security_schemes}

    ordered: list[SecurityScheme] = []
    for name in definition.global_security:
        if name in schemes_by_name:
            ordered.append(schemes_by_name[name])
    for scheme in definition.security_schemes:
        if scheme not in ordered:
            ordered.append(scheme)

    chosen: AuthTranslation | None = None
    for scheme in ordered:
        translation = _translate_scheme(scheme)
        if translation is not None and chosen is None:
            chosen = translation
        elif translation is None:
            placement = (
                f" in {scheme.location}" if scheme.type == "apiKey" and scheme.location else ""
            )
            warnings.append(
                SpecWarning(
                    code="unsupported_security_scheme",
                    message=(
                        f"Security scheme '{scheme.name}' ({scheme.type}{placement}) cannot be "
                        "mapped to a header credential and was skipped."
                    ),
                    context=scheme.name,
                )
            )

    if chosen is None:
        result = _NO_AUTH.model_copy(deep=True)
        result.warnings = warnings
        return result
    chosen.warnings = warnings
    return chosen
