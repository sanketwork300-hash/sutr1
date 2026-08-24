---
title: Google OAuth Setup
---

# Google OAuth Setup

Every Google integration shares a single Google OAuth app. You create it once and they all use the same credentials.

That covers twelve integrations, of two kinds:

| Integration | Kind | Notes |
|---|---|---|
| Gmail | REST | Sutr's own; **can send email** |
| Google Calendar | REST | Sutr's own |
| Gmail (Google MCP) | Google-hosted MCP | Drafts, threads, labels — **cannot send** |
| Google Calendar (Google MCP) | Google-hosted MCP | Adds `suggest_time` and semantic event search |
| Google Drive · Docs · Sheets · Slides · Chat · Contacts | Google-hosted MCP | |
| Google Maps Code Assist · Google Developer Knowledge | Google-hosted MCP | Cloud APIs, not Workspace |

Only enable the APIs and scopes for the ones you actually want — a user is asked to consent to the scopes of the integration they are installing, not to all of them at once.

## 1. Create a project in Google Cloud Console

1. Go to [console.cloud.google.com](https://console.cloud.google.com) and sign in.
2. Click the project selector at the top → **New Project**.
3. Give it a name (e.g. `Sutr`) and click **Create**.

## 2. Enable the APIs

From the left sidebar go to **APIs & Services → Library** and enable the APIs behind the integrations you want:

| Integration | API to enable |
|---|---|
| Gmail, Gmail (Google MCP) | Gmail API |
| Google Calendar, Google Calendar (Google MCP) | Google Calendar API |
| Google Drive | Google Drive API |
| Google Docs | Google Docs API |
| Google Sheets | Google Sheets API |
| Google Slides | Google Slides API |
| Google Chat | Google Chat API |
| Google Contacts | People API |
| Google Maps Code Assist | Maps Code Assist API |
| Google Developer Knowledge | Developer Knowledge API |

Click **Enable** on each. The Google-hosted MCP servers are ordinary Google APIs behind
`*.googleapis.com`, so they are gated by the same per-API enablement and the same OAuth
consent as the REST integrations.

## 3. Configure the OAuth consent screen

The consent screen wizard has four steps: **App info → Scopes → Test users → Summary**.

**App info:**
1. Go to **APIs & Services → OAuth consent screen**.
2. Choose **External** (or Internal if this is a Google Workspace org where all users are in the same org).
3. Fill in the required fields:
   - **App name** — anything, e.g. `Sutr`
   - **User support email** — your email
   - **Developer contact email** — your email
4. Click **Save and Continue**.

**Scopes:**
1. Click **Add or Remove Scopes**.
2. Paste each scope into the filter box and check it. Add only the ones you need:

   | Integration | Scope |
   |---|---|
   | Gmail (both) | `https://www.googleapis.com/auth/gmail.modify` |
   | Google Calendar (both) | `https://www.googleapis.com/auth/calendar` |
   | Google Drive | `https://www.googleapis.com/auth/drive` |
   | Google Docs | `https://www.googleapis.com/auth/documents` |
   | Google Sheets | `https://www.googleapis.com/auth/spreadsheets` |
   | Google Slides | `https://www.googleapis.com/auth/presentations` |
   | Google Chat | `https://www.googleapis.com/auth/chat.messages`, `https://www.googleapis.com/auth/chat.spaces.readonly` |
   | Google Contacts | `https://www.googleapis.com/auth/contacts.readonly`, `https://www.googleapis.com/auth/directory.readonly`, `.../auth/userinfo.profile`, `.../auth/userinfo.email` |
   | Maps Code Assist, Developer Knowledge | `https://www.googleapis.com/auth/cloud-platform` |

3. Click **Update** to confirm, then **Save and Continue**.

> `cloud-platform` is a broad grant covering every Google Cloud API the account can reach.
> Only the two Cloud-based integrations ask for it; skip them if that is more than you want
> to hand over.

**Test users:**
Add the Google account(s) that will connect to Sutr. While the app is in Testing mode only these accounts can authorize — you can leave it in Testing indefinitely for personal or team use.

Click **Save and Continue**, then **Back to Dashboard**.

## 4. Create OAuth credentials

1. Go to **APIs & Services → Credentials**.
2. Click **+ Create Credentials → OAuth client ID**.
3. Set **Application type** to **Web application**.
4. Give it a name, e.g. `Sutr`.
5. Under **Authorized redirect URIs**, add the value that matches your deployment:

   - **Self-hosted on a domain:** `https://sutr.example.com/api/auth/callback`
   - **Local development:** `http://localhost:4747/api/auth/callback`

   You can add both if you develop locally and also run a production deployment — they just need to match the `OAUTH_CALLBACK_URL` environment variable exactly.
6. Click **Create**.

Google will show you a **Client ID** and **Client secret** — copy both.

## 5. Set the environment variables

```bash
export OAUTH_GOOGLE_CLIENT_ID="<your client id>"
export OAUTH_GOOGLE_CLIENT_SECRET="<your client secret>"
```

Or add them to your `.env` file:

```
OAUTH_GOOGLE_CLIENT_ID=<your client id>
OAUTH_GOOGLE_CLIENT_SECRET=<your client secret>
```

All twelve Google integrations become available in the integrations UI once these are set —
availability is gated on the credentials existing, not on which APIs you enabled. If you
install one whose API you have not enabled, Google refuses the call and Sutr surfaces that
error rather than masking it.

> These are **not** the same as `GOOGLE_LOGIN_CLIENT_ID` / `GOOGLE_LOGIN_CLIENT_SECRET`,
> which only let people *sign in to Sutr* with Google. You can point both at the same
> Google OAuth client — Google allows several authorized redirect URIs on one client — but
> you must then register both callbacks:
>
> - integrations: `{BASE_URL}/api/auth/callback` (whatever `OAUTH_CALLBACK_URL` is)
> - sign-in: `{BASE_URL}/api/auth/google/callback`

## Notes

**Refresh tokens** — The OAuth flow requests `access_type=offline` and `prompt=consent`, so Google issues a refresh token on first authorization. Tokens are refreshed automatically when they expire.

**Testing vs. Published** — Apps in Testing mode only allow the test users you explicitly add. For personal or team use this is fine indefinitely. To allow any Google account to connect, submit the app for verification (required when using sensitive scopes like Gmail and Calendar).

**Callback URL in production** — If you deploy Sutr to a non-localhost URL, add that callback URL to the **Authorized redirect URIs** list in your OAuth client and set `OAUTH_CALLBACK_URL` accordingly. You can have multiple redirect URIs registered on the same client.
