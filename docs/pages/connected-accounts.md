---
title: Connected accounts
nav_title: Connected accounts
---
# Connected accounts

A **connected account** is an OAuth grant Sutr holds on your behalf. Two things need one, and underneath they are the same thing:

| Provider | Kind | What it lets Sutr do |
|---|---|---|
| GitHub | spec source | List the repositories you can already see, and read OpenAPI documents out of them — including private ones — without you pasting a personal access token. |
| Google Cloud | deploy target | Build and run a generated MCP server on Cloud Run, in a project you choose. |
| Microsoft Azure | deploy target | Build and run a generated MCP server on Container Apps, in a subscription you choose. |
| Amazon Web Services | deploy target | Build and run a generated MCP server on App Runner, in an account and role you choose. |

Manage them in **Settings → Connected accounts**, or from the terminal with `sutr connections`.

## What is stored, and where

Access and refresh tokens are written through the configured [secrets backend](/self-host/configure) as `secret` rows, exactly like an integration credential. The `provider_connection` row itself holds only pointers to those secrets, an expiry, the granted scopes, and a display label such as your GitHub login. Nothing token-shaped ever appears in an API response.

A connection is **personal**: it is scoped to one user inside one organization. Other members can see that a connection exists, but cannot borrow it to deploy under your cloud identity, and cannot revoke it. An API key cannot open one at all — an API key belongs to the organization, not to a person, so a connection made with one would have no identity to revoke.

**Disconnecting deletes the tokens from Sutr. It does not revoke the app** on the provider's side; only you can do that, from GitHub's *Authorized OAuth Apps*, Google's *Third-party apps with account access*, or the equivalent. The UI says so where you click.

## The two flows

**Authorization code with PKCE** — GitHub, Google Cloud, Azure. Sutr stores a single-use `state` row, sends you to the provider, and the provider redirects back to `{BASE_URL}/api/connections/{provider}/callback`. That callback is the only place the code is accepted, the state row is deleted before the exchange whatever the outcome, expires after 15 minutes, and the post-connect redirect is restricted to a relative path inside your own UI — an open redirect on a callback is how an OAuth flow becomes a phishing hop.

In the browser the consent screen opens in a popup, so the MCP builder does not lose a half-filled form. If the popup is blocked the flow falls back to a same-tab redirect.

**Device authorization grant (RFC 8628)** — AWS. Amazon publishes no OAuth interface to its own service APIs, so the OAuth path for AWS is IAM Identity Center's `sso-oidc` endpoint, which does implement the device grant:

1. Sutr registers a throwaway public client.
2. It opens a device authorization and shows you a confirmation code.
3. You approve the code in your AWS access portal.
4. Sutr polls until Identity Center issues a token.

Only that Identity Center token is stored. Every AWS operation exchanges it for **short-lived role credentials** (`GetRoleCredentials`), which expire within the hour and are re-fetched per operation. That is the whole reason to prefer this over storing an access key pair.

## Expiry and refresh

Every call goes through one function that refreshes the access token when it is within two minutes of expiry, so a token never expires mid-deploy. Providers differ in ways that matter:

- **GitHub** OAuth-app tokens do not expire and send no `expires_in`; Sutr treats a missing expiry as "no expiry" rather than refreshing on every call.
- **Google** returns a refresh token only when asked for `access_type=offline` with `prompt=consent`, which Sutr always does — and it omits the refresh token on re-consent, so re-authorizing never blanks the one already stored.
- **AWS** device sessions simply end. There is nothing to refresh with, so an expired one is reported as `reauthorization_required` and the UI asks you to reconnect.

## Operator setup

Each provider needs an OAuth app registered once by whoever runs the server. Until then it is reported as *not configured* — with the exact environment variables and callback URL in the message — rather than offering a button that fails. See [Connected accounts setup](/self-host/connected-accounts) for the registration steps.

## From the CLI

```bash
sutr connections list                       # who is connected, and what is not set up
sutr connections connect github             # prints a URL to open
sutr connections connect aws                # shows a code and polls until approved
sutr connections targets <connection_id>    # projects / subscriptions / accounts
sutr connections disconnect <connection_id>
```

The authorization-code providers need a browser, so `connect` prints the URL and stops. The AWS device grant was designed for exactly this situation, which is why it is the one flow the CLI finishes end to end.
