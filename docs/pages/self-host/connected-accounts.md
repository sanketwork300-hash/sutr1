---
title: Connected accounts setup
nav_title: Connected accounts
---
# Connected accounts setup

Registering the OAuth apps that let your users connect GitHub and cloud accounts. Each is optional and independent: a provider with no credentials configured is reported as *not configured* — naming the exact variables and the callback URL — instead of showing a button that fails.

Everything below sets environment variables on the **server**. `BASE_URL` must be the address the provider can redirect a browser back to; on a local install that is `http://localhost:4747`.

---

## GitHub — reading specifications

Lets users import OpenAPI documents from repositories they can already see, including private ones, without pasting a personal access token.

1. Go to **GitHub → Settings → Developer settings → OAuth Apps → New OAuth App**.
2. **Homepage URL**: your Sutr UI address.
3. **Authorization callback URL**: `{BASE_URL}/api/connections/github/callback` — this must match exactly.
4. Generate a client secret.

```bash
GITHUB_OAUTH_CLIENT_ID=Iv1.xxxxxxxxxxxx
GITHUB_OAUTH_CLIENT_SECRET=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
# Optional. "repo read:user" is the default and is required to read private
# repositories. Narrow it to "public_repo read:user" for a public-only install.
GITHUB_OAUTH_SCOPE="repo read:user"
```

Sutr only ever reads: it lists repositories, walks a tree, and fetches file contents. It never writes to a repository.

> A GitHub App would allow per-repository selection, which is tighter than an OAuth App's account-wide `repo` scope. Sutr uses an OAuth App today; if that grant is broader than you want, leave GitHub unconfigured and let users paste a fine-grained personal access token instead — that path is fully supported and the token is never stored.

---

## Google Cloud — deploying to Cloud Run

1. In the **Google Cloud console → APIs & Services → Credentials**, create an **OAuth client ID** of type *Web application*.
2. **Authorized redirect URI**: `{BASE_URL}/api/connections/gcp/callback`.
3. On the OAuth consent screen, add the scope `https://www.googleapis.com/auth/cloud-platform`.
4. Enable the APIs the deploy uses in each target project: **Cloud Run**, **Cloud Build**, **Artifact Registry**, and **Cloud Storage**.

```bash
GCP_OAUTH_CLIENT_ID=xxxxxxxx.apps.googleusercontent.com
GCP_OAUTH_CLIENT_SECRET=xxxxxxxxxxxxxxxxxx
```

This is deliberately separate from `GOOGLE_LOGIN_CLIENT_ID` / `GOOGLE_LOGIN_CLIENT_SECRET`, which only authenticate a user into Sutr. `cloud-platform` is a far larger grant than sign-in and should not ride on the same consent.

**What a deploy creates** in the chosen project: an Artifact Registry Docker repository (if missing), one Cloud Build build, and a Cloud Run service with a public invoker binding. If an organization policy blocks `allUsers` — usually *Domain restricted sharing* — the deploy fails with that named as the cause rather than leaving a service nobody can call.

---

## Microsoft Azure — deploying to Container Apps

1. **Azure portal → Microsoft Entra ID → App registrations → New registration**.
2. **Redirect URI** (type *Web*): `{BASE_URL}/api/connections/azure/callback`.
3. Under **API permissions**, add *Azure Service Management → user_impersonation* (delegated).
4. Create a client secret under **Certificates & secrets**.

```bash
AZURE_OAUTH_CLIENT_ID=00000000-0000-0000-0000-000000000000
AZURE_OAUTH_CLIENT_SECRET=xxxxxxxxxxxxxxxxxxxxxxx
# "organizations" lets any work or school tenant consent. Pin a tenant id for
# a single-tenant install.
AZURE_OAUTH_TENANT=organizations
```

**What a deploy creates**: a resource group, an Azure Container Registry, and a Container Apps managed environment (each only if missing), one image build via ACR Tasks, and a Container App with external ingress. The registry's admin user is enabled so the app can pull with a username and password — a managed identity would be tidier but needs a role assignment on the subscription that many users' accounts cannot make.

Registry names are **globally unique**, 5–50 lowercase alphanumeric characters, no hyphens. Sutr validates that before creating anything.

---

## Amazon Web Services — deploying to App Runner

AWS has no OAuth interface to its own service APIs. Sutr uses the one place AWS really does speak OAuth: **IAM Identity Center**'s `sso-oidc` device authorization grant. There is no client secret to register — the client is created dynamically per attempt.

```bash
# Your IAM Identity Center portal URL.
AWS_SSO_START_URL=https://d-1234567890.awsapps.com/start
AWS_SSO_REGION=us-east-1
```

Users then run the device flow, pick an account and a permission set, and Sutr exchanges the Identity Center token for short-lived role credentials on every operation.

### The two IAM roles a user must create

App Runner and CodeBuild both require roles that Sutr cannot invent. They are asked for explicitly in the deploy form, once per account:

**CodeBuild service role** — assumed by `codebuild.amazonaws.com`. Needs:

- `s3:GetObject` on the staging bucket (`sutr-mcp-{account}-{region}`),
- `ecr:GetAuthorizationToken`, plus `ecr:BatchCheckLayerAvailability`, `ecr:InitiateLayerUpload`, `ecr:UploadLayerPart`, `ecr:CompleteLayerUpload`, `ecr:PutImage` on the repository,
- `logs:CreateLogGroup`, `logs:CreateLogStream`, `logs:PutLogEvents`.

**App Runner ECR access role** — assumed by `build.apprunner.amazonaws.com`, with the AWS-managed policy `AWSAppRunnerServicePolicyForECRAccess` attached.

The Identity Center permission set the user picks additionally needs S3, ECR, CodeBuild, App Runner, CloudWatch Logs, and `iam:PassRole` for those two roles.

**What a deploy creates**: an S3 bucket and ECR repository (if missing), a CodeBuild project and one build, and an App Runner service with a public HTTPS URL.

---

## Turning providers off

Each cloud provider can be disabled independently, regardless of whether its OAuth app is configured:

```bash
DEPLOY_GCP_ENABLED=false
DEPLOY_AZURE_ENABLED=false
DEPLOY_AWS_ENABLED=false
DEPLOY_DOCKER_ENABLED=false   # the local provider; always off on cloud instances
```

## Verifying

```bash
sutr connections list          # per-provider: configured, connected, or why not
sutr deploy providers          # deployment targets and the fields each needs
```

An unconfigured provider prints the missing environment variables and the exact callback URL to register.
