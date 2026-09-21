/**
 * A failure the user can act on.
 *
 * Every connect failure carries the real reason — the server's message, the
 * rejected path, the status — plus the one command that moves the user
 * forward. Nothing here ever collapses into "something went wrong".
 */
export class ConnectError extends Error {
  readonly title: string;
  readonly reason: string;
  readonly hint?: string;

  constructor(title: string, reason: string, hint?: string) {
    super(`${title}: ${reason}`);
    this.name = "ConnectError";
    this.title = title;
    this.reason = reason;
    this.hint = hint;
  }
}

/** The user pressed ctrl-c, or declined a change. Not an error worth a stack. */
export class ConnectCancelled extends Error {
  constructor(message = "Cancelled.") {
    super(message);
    this.name = "ConnectCancelled";
  }
}
