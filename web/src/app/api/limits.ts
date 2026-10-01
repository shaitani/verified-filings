import { HttpErrorResponse } from '@angular/common/http';

// The server's rate-limit refusals (app/api/limits.py), in the reader's words. Each is
// checked before a page's own codes: SERVER_BUSY is a 503, which would otherwise read
// as an outage.
const LIMITS: Readonly<Record<string, (wait: string) => string>> = {
  TOO_MANY_ATTEMPTS: (wait) => `Too many sign-in attempts. Please try again in ${wait}.`,
  TOO_MANY_QUESTIONS: () =>
    'You already have questions waiting. Ask again once one of them finishes.',
  DAILY_LIMIT: (wait) =>
    `You have reached today's limit of questions. You can ask again in ${wait}.`,
  SERVER_BUSY: (wait) => `The server is busy with other questions. Please try again in ${wait}.`,
};

/** The sentence for a rate-limit refusal, or null for anything else. */
export function limitProblem(error: unknown): string | null {
  if (!(error instanceof HttpErrorResponse)) return null;
  const detail: unknown = error.error?.detail;
  if (typeof detail !== 'string' || !(detail in LIMITS)) return null;
  return LIMITS[detail](waitFor(error.headers.get('Retry-After')));
}

/** `Retry-After` (whole seconds) as words: "a minute", "12 minutes", "3 hours". */
export function waitFor(retryAfter: string | null): string {
  const seconds = Number(retryAfter);
  if (!retryAfter || !Number.isFinite(seconds) || seconds <= 0) return 'a little while';
  if (seconds <= 60) return 'a minute';
  if (seconds < 3600) return `${Math.ceil(seconds / 60)} minutes`;
  const hours = Math.ceil(seconds / 3600);
  return hours === 1 ? 'an hour' : `${hours} hours`;
}
