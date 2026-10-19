import { chmodSync, mkdtempSync, readFileSync, statSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, describe, expect, it, vi } from "vitest";
import { isZipArchive, suggestedZipName } from "../src/commands/openapi.js";
import { RedirectRefusedError, request } from "../src/client.js";
import { isInsecureTransport } from "../src/connect/checks.js";
import {
  InvalidConfigError,
  credentialHeaderValue,
  instanceBaseUrl,
  maskKey,
  type Config,
} from "../src/config.js";

const config: Config = {
  url: "https://sutr.test",
  auth_mode: "api_key",
  output_format: "human",
  api_key: "ap_notarealkey",
  access_token: "",
  refresh_token: "",
  oauth_client_id: "",
};

describe("package download file name", () => {
  it("honours a plain zip name from Content-Disposition", () => {
    expect(suggestedZipName('attachment; filename="stripe-mcp.zip"')).toBe("stripe-mcp.zip");
    expect(suggestedZipName('attachment; filename="v1.2_build.zip"')).toBe("v1.2_build.zip");
  });

  it("falls back when the header is missing or not a zip", () => {
    expect(suggestedZipName("")).toBe("mcp-server.zip");
    expect(suggestedZipName("attachment")).toBe("mcp-server.zip");
    expect(suggestedZipName('attachment; filename="server.tar.gz"')).toBe("mcp-server.zip");
    expect(suggestedZipName('attachment; filename=".zip"')).toBe("mcp-server.zip");
  });

  it("never writes outside the working directory", () => {
    // Directories are dropped, so a traversal lands in the cwd under its leaf
    // name; a leaf that is not a plain zip name is replaced outright.
    expect(suggestedZipName('attachment; filename="../../.bashrc"')).toBe("mcp-server.zip");
    expect(suggestedZipName('attachment; filename="../../evil.zip"')).toBe("evil.zip");
    expect(suggestedZipName('attachment; filename="/etc/cron.d/x.zip"')).toBe("x.zip");
    expect(suggestedZipName('attachment; filename="..\\\\..\\\\x.zip"')).toBe("x.zip");
    expect(suggestedZipName('attachment; filename="a b.zip"')).toBe("mcp-server.zip");
  });
});

describe("config file permissions", () => {
  it("creates the directory 0700 and the file 0600, tightening an existing file", async () => {
    if (process.platform === "win32") return;
    const home = mkdtempSync(join(tmpdir(), "sutr-cli-home-"));
    vi.doMock("node:os", () => ({ homedir: () => home }));
    vi.resetModules();
    const { writeConfig } = await import("../src/config.js");
    const dir = join(home, ".config", "sutr");
    const path = join(dir, "config.json");

    writeConfig(config);
    expect(statSync(dir).mode & 0o777).toBe(0o700);
    expect(statSync(path).mode & 0o777).toBe(0o600);
    expect(JSON.parse(readFileSync(path, "utf-8")).api_key).toBe(config.api_key);

    // A file left world-readable by an older release is fixed on the next write.
    chmodSync(path, 0o644);
    expect(statSync(path).mode & 0o777).toBe(0o644);
    writeConfig(config);
    expect(statSync(path).mode & 0o777).toBe(0o600);
    vi.doUnmock("node:os");
  });
});

describe("redirects", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("does not follow a redirect that would forward the API key", async () => {
    const fetchMock = vi.fn(async (_input: unknown, init: RequestInit = {}) => {
      expect(init.redirect).toBe("manual");
      return {
        ok: false,
        status: 302,
        statusText: "Found",
        body: null,
        headers: new Headers({ location: "https://evil.test/api/tools" }),
        json: async () => ({}),
      } as unknown as Response;
    });
    vi.stubGlobal("fetch", fetchMock);
    await expect(request("/api/tools", { config })).rejects.toBeInstanceOf(RedirectRefusedError);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});

describe("insecure transport", () => {
  it("flags plain http to anything that is not loopback", () => {
    expect(isInsecureTransport(new URL("http://sutr.internal:4747"))).toBe(true);
    expect(isInsecureTransport(new URL("http://10.0.0.5"))).toBe(true);
    expect(isInsecureTransport(new URL("http://localhost:4747"))).toBe(false);
    expect(isInsecureTransport(new URL("http://127.0.0.1:4747"))).toBe(false);
    expect(isInsecureTransport(new URL("http://[::1]:4747"))).toBe(false);
    expect(isInsecureTransport(new URL("https://sutr.internal"))).toBe(false);
  });
});

describe("package download content", () => {
  it("accepts only a zip archive", () => {
    expect(isZipArchive(Buffer.from("PK\x03\x04rest", "latin1"))).toBe(true);
    expect(isZipArchive(Buffer.from("PK\x05\x06", "latin1"))).toBe(true);
    expect(isZipArchive(Buffer.from("#!/bin/sh\nrm -rf ~\n"))).toBe(false);
    expect(isZipArchive(Buffer.from('{"detail":"oops"}'))).toBe(false);
    expect(isZipArchive(Buffer.alloc(0))).toBe(false);
  });
});

describe("values read from the config", () => {
  it("normalises a well-formed instance URL", () => {
    expect(instanceBaseUrl({ url: " https://sutr.test/ " })).toBe("https://sutr.test");
    expect(instanceBaseUrl({ url: "http://localhost:4747/base///" })).toBe(
      "http://localhost:4747/base",
    );
  });

  it("refuses an instance URL the credential must not be sent to", () => {
    for (const url of [
      "",
      "not a url",
      "file:///etc/passwd",
      "ftp://sutr.test",
      "https://user:pass@sutr.test",
      "https://sutr.test/?x=1",
      "https://sutr.test/#frag",
    ]) {
      expect(() => instanceBaseUrl({ url })).toThrow(InvalidConfigError);
    }
  });

  it("refuses a request to an invalid URL before any network call", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    await expect(
      request("/api/tools", { config: { ...config, url: "javascript:alert(1)" } }),
    ).rejects.toBeInstanceOf(InvalidConfigError);
    expect(fetchMock).not.toHaveBeenCalled();
    vi.unstubAllGlobals();
  });

  it("accepts a token-shaped API key and rejects anything that could break a header", () => {
    expect(credentialHeaderValue("ap_Ab-9_xYz")).toBe("ap_Ab-9_xYz");
    for (const key of ["ap_x\r\nX-Evil: 1", "ap_x\n", "ap x", "ap_\u00e9", ""]) {
      expect(() => credentialHeaderValue(key)).toThrow(InvalidConfigError);
    }
  });

  it("masks a short key entirely", () => {
    expect(maskKey("ap_short")).toBe("***");
    expect(maskKey("ap_notarealkey")).toBe("ap_not...lkey");
  });
});
