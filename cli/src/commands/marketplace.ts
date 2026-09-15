import { Command } from "commander";
import { request } from "../client.js";
import { print, printError, resolveFormat } from "../output.js";

export const marketplaceCommand = new Command("marketplace").description(
  "Browse the tool catalogue",
);

interface Listing {
  integration_id: string;
  name: string;
  description: string | null;
  type: string;
  category: string;
  tags: string[];
  tool_count: number | null;
  install_count: number;
  installed: boolean;
  available: boolean;
  available_reason: string | null;
  rating: number | null;
  review_count: number;
  trust_score: number | null;
  pending_fields: string[];
  pending_reason: string | null;
}

interface ListingPage {
  total: number;
  limit: number;
  offset: number;
  sort: string;
  listings: Listing[];
}

marketplaceCommand
  .command("search [query]", { isDefault: true })
  .description("Search and filter the catalogue")
  .option("--category <name>", "Filter by category")
  .option("--tag <tag>", "Filter by tag (repeatable)", (v: string, all: string[]) => [...all, v], [])
  .option("--type <type>", "remote_mcp or custom")
  .option("--installed", "Only what this organization has installed")
  .option("--sort <sort>", "popular | rating | name", "popular")
  .option("--limit <n>", "How many to return", "25")
  .option("-o, --output <format>", "Output format")
  .action(
    async (
      query: string | undefined,
      opts: {
        category?: string;
        tag: string[];
        type?: string;
        installed?: boolean;
        sort: string;
        limit: string;
        output: string;
      },
    ) => {
      const format = resolveFormat(opts.output);
      const params: Record<string, string> = { sort: opts.sort, limit: opts.limit };
      if (query) params.q = query;
      if (opts.category) params.category = opts.category;
      if (opts.type) params.type = opts.type;
      if (opts.installed) params.installed = "true";
      try {
        // URLSearchParams cannot express a repeated key through this shape, so
        // repeated tags are appended to the path directly.
        const repeated = opts.tag.map((t) => `tag=${encodeURIComponent(t)}`).join("&");
        const page = await request<ListingPage>(
          `/api/marketplace/listings${repeated ? `?${repeated}` : ""}`,
          { params },
        );
        if (format !== "human") {
          print(page as unknown as Record<string, unknown>, format);
          return;
        }
        print(
          page.listings.map((l) => ({
            id: l.integration_id,
            name: l.name,
            category: l.category,
            installs: l.install_count,
            // "-" rather than 0: nobody has rated it, which is not the same
            // as everybody rating it zero.
            rating: l.rating ?? "-",
            installed: l.installed,
          })),
          format,
        );
      } catch (e) {
        printError((e as Error).message, 1);
      }
    },
  );

marketplaceCommand
  .command("show <integration_id>")
  .description("Everything the marketplace knows about one listing")
  .option("-o, --output <format>", "Output format")
  .action(async (integrationId: string, opts: { output: string }) => {
    const format = resolveFormat(opts.output);
    try {
      const listing = await request<Listing>(
        `/api/marketplace/listings/${encodeURIComponent(integrationId)}`,
      );
      if (format !== "human") {
        print(listing as unknown as Record<string, unknown>, format);
        return;
      }
      print(
        {
          id: listing.integration_id,
          name: listing.name,
          description: listing.description ?? "-",
          type: listing.type,
          category: listing.category,
          tags: listing.tags.join(", ") || "-",
          tools: listing.tool_count ?? "discovered on connect",
          installs: listing.install_count,
          installed: listing.installed,
          rating: listing.rating === null ? "not rated" : `${listing.rating} (${listing.review_count})`,
          available: listing.available ? "yes" : (listing.available_reason ?? "no"),
          not_available_yet: listing.pending_fields.join(", ") || "-",
        },
        format,
      );
    } catch (e) {
      printError((e as Error).message, 1);
    }
  });

marketplaceCommand
  .command("categories")
  .description("Categories, with how many listings each holds")
  .option("-o, --output <format>", "Output format")
  .action(async (opts: { output: string }) => {
    const format = resolveFormat(opts.output);
    try {
      const facets = await request<{ category: string; count: number }[]>(
        "/api/marketplace/categories",
      );
      print(facets as unknown as Record<string, unknown>[], format);
    } catch (e) {
      printError((e as Error).message, 1);
    }
  });

marketplaceCommand
  .command("rate <integration_id> <rating>")
  .description("Rate a listing from 1 to 5")
  .option("--note <text>", "Optional review body")
  .option("-o, --output <format>", "Output format")
  .action(
    async (integrationId: string, rating: string, opts: { note?: string; output: string }) => {
      const format = resolveFormat(opts.output);
      const value = Number(rating);
      if (!Number.isInteger(value) || value < 1 || value > 5) {
        printError("The rating must be a whole number from 1 to 5.", 1);
      }
      try {
        const review = await request<Record<string, unknown>>(
          `/api/marketplace/listings/${encodeURIComponent(integrationId)}/reviews`,
          { method: "PUT", body: { rating: value, body: opts.note } },
        );
        print(review, format);
      } catch (e) {
        printError((e as Error).message, 1);
      }
    },
  );
