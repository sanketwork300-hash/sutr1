import { chmodSync, readFileSync, writeFileSync, mkdirSync } from "node:fs";
import { join } from "node:path";
import { homedir } from "node:os";

export type AuthMode = "" | "oauth" | "api_key";
export type OutputFormat = "human" | "json" | "toon";

export interface Config {
  url: string;
  auth_mode: AuthMode;
  output_format: OutputFormat;
  api_key: string;
  access_token: string;
  refresh_token: string;
  oauth_client_id: string;
}

const CONFIG_DIR = join(homedir(), ".config", "sutr");
const CONFIG_PATH = join(CONFIG_DIR, "config.json");
// Pre-rebrand (AgentPort) config location, read once as a fallback so an
// existing login survives the upgrade; the next write lands in CONFIG_PATH.
const LEGACY_CONFIG_PATH = join(homedir(), ".config", "agent-port", "config.json");

const DEFAULT_URL = process.env.SUTR_URL || process.env.AGENT_PORT_URL || "https://app.sutr.sh";

const DEFAULTS: Config = {
  url: DEFAULT_URL,
  auth_mode: "",
  output_format: "human",
  api_key: "",
  access_token: "",
  refresh_token: "",
  oauth_client_id: "",
};

export function readConfig(): Config {
  try {
    const raw = readFileSync(CONFIG_PATH, "utf-8");
    return { ...DEFAULTS, ...JSON.parse(raw) };
  } catch {
    try {
      const raw = readFileSync(LEGACY_CONFIG_PATH, "utf-8");
      return { ...DEFAULTS, ...JSON.parse(raw) };
    } catch {
      return { ...DEFAULTS };
    }
  }
}

/**
 * The config holds the API key, so it is private to the user: the directory is
 * 0700, the file 0600, and a file created by an older release at the umask
 * default is tightened on its next write rather than left readable.
 */
export function writeConfig(config: Config): void {
  mkdirSync(CONFIG_DIR, { recursive: true, mode: 0o700 });
  writeFileSync(CONFIG_PATH, JSON.stringify(config, null, 2) + "\n", { mode: 0o600 });
  try {
    chmodSync(CONFIG_PATH, 0o600);
  } catch {
    // Windows has no POSIX mode bits; the file is still private to the profile.
  }
}

/**
 * Wipe every credential field. Call this before switching auth modes so stale
 * state from the prior mode can't leak into the new session.
 */
export function clearCredentials(config: Config): void {
  config.auth_mode = "";
  config.api_key = "";
  config.access_token = "";
  config.refresh_token = "";
  config.oauth_client_id = "";
}

export function maskKey(key: string): string {
  if (!key) return "";
  // A key this short is malformed anyway; any slice of it would be most of it.
  if (key.length <= 8) return "***";
  return key.slice(0, 6) + "..." + key.slice(-4);
}

/** A value in the config that the CLI refuses to send anywhere. */
export class InvalidConfigError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "InvalidConfigError";
  }
}

export function stripTrailingSlashes(url: string): string {
  let end = url.length;
  while (end > 0 && url.charCodeAt(end - 1) === 47 /* "/" */) end--;
  return url.slice(0, end);
}

/**
 * The instance URL, checked before anything is sent to it.
 *
 * The config file is plain JSON that anything running as the user can edit,
 * and every request carries the stored credential to this address, so it has
 * to be an absolute http(s) URL with no userinfo, query, or fragment for the
 * request path to be appended to. Returned without trailing slashes.
 */
export function instanceBaseUrl(config: Pick<Config, "url">): string {
  const raw = typeof config.url === "string" ? config.url.trim() : "";
  let parsed: URL;
  try {
    parsed = new URL(raw);
  } catch {
    throw new InvalidConfigError(`'${raw}' is not a valid URL.`);
  }
  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
    throw new InvalidConfigError(
      `'${raw}' must use http or https, not '${parsed.protocol.replace(":", "")}'.`,
    );
  }
  if (parsed.username || parsed.password) {
    throw new InvalidConfigError(`The URL for '${parsed.host}' must not embed a username or password.`);
  }
  if (parsed.search || parsed.hash) {
    throw new InvalidConfigError(`'${raw}' must not carry a query string or fragment.`);
  }
  return stripTrailingSlashes(parsed.origin + parsed.pathname);
}

// RFC 7235 token68: what API keys (`ap_` + urlsafe base64) and bearer tokens
// are made of. Nothing that could end the header it travels in.
const CREDENTIAL_PATTERN = /^[A-Za-z0-9._~+/=-]+$/;

/** An API key, checked before it is stored or put in a request header. */
export function credentialHeaderValue(value: string): string {
  if (typeof value !== "string" || !CREDENTIAL_PATTERN.test(value)) {
    throw new InvalidConfigError(
      "The API key is malformed: expected a single token such as ap_..., with no " +
        "whitespace or control characters. Run `sutr auth login --api-key <key>` with a valid key.",
    );
  }
  return value;
}
