"""Marketplace categories and tags for the bundled integrations.

Kept as one curated data file rather than a field on each of the 59
integration modules: a category is a *marketplace* concern, it changes when the
storefront's taxonomy changes, and editing 59 files to rename one category
would be absurd. Integrations that define their own `category`/`tags` override
what is here.

An integration missing from this map is not an error — it appears under
`UNCATEGORIZED`, which is honest and visible, rather than being hidden or
guessed into a category by keyword matching.
"""

UNCATEGORIZED = "Other"

# Category → the integrations in it. One category per integration; tags carry
# the secondary facets.
CATEGORIES: dict[str, tuple[str, ...]] = {
    "Analytics": ("amplitude", "mixpanel", "posthog", "thoughtspot"),
    "Communication": (
        "gmail",
        "gmail_mcp",
        "google_chat",
        "google_contacts",
        "intercom",
        "resend",
        "slack",
        "telnyx",
    ),
    "CRM & Sales": ("attio", "close", "square"),
    "Developer Tools": (
        "github",
        "huggingface",
        "linear",
        "prisma",
        "semgrep",
        "zapier",
        "google_developer_knowledge",
        "google_maps_code_assist",
    ),
    "Documents & Storage": (
        "cloudinary",
        "contentful",
        "egnyte",
        "google_docs",
        "google_drive",
        "google_sheets",
        "google_slides",
        "notion",
        "sanity",
    ),
    "Finance": ("mercury", "ramp", "stripe"),
    "Identity & Security": ("stytch",),
    "Infrastructure": (
        "cloudflare",
        "neon",
        "netlify",
        "render",
        "supabase",
        "vercel",
    ),
    "Observability": ("axiom", "datadog", "sentry", "launchdarkly"),
    "Productivity": (
        "asana",
        "atlassian",
        "calendly",
        "fireflies",
        "google_calendar",
        "google_calendar_mcp",
        "granola",
        "monday",
        "tally",
    ),
    "Search & Data": ("apify", "exa"),
    "Website Builders": ("webflow", "wix"),
}

# Secondary facets. An integration may carry several.
TAGS: dict[str, tuple[str, ...]] = {
    "google": (
        "gmail",
        "gmail_mcp",
        "google_calendar",
        "google_calendar_mcp",
        "google_chat",
        "google_contacts",
        "google_developer_knowledge",
        "google_docs",
        "google_drive",
        "google_maps_code_assist",
        "google_sheets",
        "google_slides",
    ),
    "email": ("gmail", "gmail_mcp", "resend"),
    "calendar": ("calendly", "google_calendar", "google_calendar_mcp"),
    "payments": ("mercury", "ramp", "square", "stripe"),
    "database": ("neon", "prisma", "supabase"),
    "deployment": ("cloudflare", "netlify", "render", "vercel"),
    "issue-tracking": ("atlassian", "github", "linear", "sentry"),
    "meetings": ("fireflies", "granola"),
    "cms": ("contentful", "sanity", "webflow", "wix"),
    "monitoring": ("axiom", "datadog", "sentry"),
    "security": ("semgrep", "stytch"),
    "ai": ("exa", "huggingface"),
}

_BY_INTEGRATION: dict[str, str] = {
    integration_id: category
    for category, members in CATEGORIES.items()
    for integration_id in members
}

_TAGS_BY_INTEGRATION: dict[str, list[str]] = {}
for _tag, _members in TAGS.items():
    for _integration_id in _members:
        _TAGS_BY_INTEGRATION.setdefault(_integration_id, []).append(_tag)


def category_for(integration) -> str:
    """The marketplace category for an integration.

    A per-integration `category` attribute wins, then this map, then `Other`.
    Custom (per-org) integrations carry their own type as the category, since
    a user's imported API has no place in a curated taxonomy.
    """
    declared = getattr(integration, "category", None)
    if declared:
        return declared
    integration_id = getattr(integration, "id", "")
    if integration_id in _BY_INTEGRATION:
        return _BY_INTEGRATION[integration_id]
    if integration_id.startswith("customapi_"):
        return "Your APIs"
    if integration_id.startswith("custom_"):
        return "Your MCP servers"
    return UNCATEGORIZED


def tags_for(integration) -> list[str]:
    """Secondary facets: curated tags plus the integration's tool groupings.

    Tool-category names make good filters — an integration that groups its
    tools under "Issues" is meaningfully tagged `issues` — and they come from
    the integration itself rather than from this file.
    """
    declared = list(getattr(integration, "tags", None) or [])
    integration_id = getattr(integration, "id", "")
    curated = _TAGS_BY_INTEGRATION.get(integration_id, [])
    from_tools = {
        str(label).strip().lower().replace(" ", "-")
        for label in (getattr(integration, "tool_categories", None) or {}).values()
        if str(label).strip()
    }
    return sorted({*declared, *curated, *from_tools})


def all_categories() -> list[str]:
    return sorted(CATEGORIES) + [UNCATEGORIZED]
