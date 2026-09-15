"""The Marketplace: a read-optimized storefront derived from Registry events.

LLD §3.7 draws the line and this package keeps to it. The registry is the
record; this is a projection of it, plus the two things that are genuinely the
storefront's own — subscriptions and provider profiles.

    projection     event handlers that build `marketplace_listing`
    listings       reading the projection: search, filter, rank
    subscriptions  Discover → Subscribe → Provision → Use → Renew → Cancel
    profiles       how a provider presents itself
    events         subscription.created, review.created, rating.updated

The projection can lag, and every listing says when it was projected and from
which event. Nothing authorizes off a listing: subscribing re-reads the
registry, because a storefront row that is one event behind must not be the
thing that decides who gets access.
"""
