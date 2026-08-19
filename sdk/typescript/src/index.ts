/**
 * Sutr TypeScript SDK — call governed tools from an agent.
 *
 * ```ts
 * import { Sutr, ApprovalRequired } from '@sutr/sdk';
 *
 * const sutr = new Sutr({ apiKey: 'ap_...', baseUrl: 'https://sutr.example.com' });
 *
 * try {
 *   const result = await sutr.callTool('stripe', 'create_refund', { charge: 'ch_1' });
 *   console.log(result.text);
 * } catch (error) {
 *   if (error instanceof ApprovalRequired) {
 *     console.log(`A human must approve this: ${error.approvalUrl}`);
 *   }
 * }
 * ```
 *
 * Upstream credentials never leave the Sutr server; the approval policy,
 * audit trail, and usage metering apply to every call.
 */

export { Sutr } from './client.js';
export {
  APIError,
  ApprovalPending,
  ApprovalRequired,
  AuthenticationError,
  NotFoundError,
  PermissionDenied,
  RateLimited,
  ServerError,
  SutrConnectionError,
  SutrError,
  ToolDenied,
} from './errors.js';
export type {
  ApprovalDecision,
  ApprovalStatus,
  CallToolOptions,
  ContentBlock,
  Integration,
  JsonSchema,
  LogsQuery,
  SutrOptions,
  Tool,
  ToolResult,
  UsageEventsQuery,
  UsageQuery,
} from './types.js';

export const VERSION = '0.1.0';
