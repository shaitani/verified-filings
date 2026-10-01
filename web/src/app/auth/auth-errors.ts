import { HttpErrorResponse } from '@angular/common/http';

import { limitProblem } from '../api/limits';

/** Which sign-in step failed: the same code can mean different things to the reader. */
export type SignInStep = 'login' | 'register' | 'github';

// FastAPI Users' error codes, plus the server's own INVITE_REQUIRED (app/api/auth.py).
const SENTENCES: Readonly<Record<string, string>> = {
  LOGIN_BAD_CREDENTIALS: 'That email and password do not match an account.',
  LOGIN_USER_NOT_VERIFIED: 'This account has not been verified yet.',
  REGISTER_USER_ALREADY_EXISTS: 'An account with that email already exists. Sign in instead.',
  OAUTH_USER_ALREADY_EXISTS:
    "An account already uses this GitHub account's email. Sign in with that email and password.",
  OAUTH_NOT_AVAILABLE_EMAIL: 'GitHub did not share an email address for this account.',
  OAUTH_INVALID_STATE: 'That GitHub sign-in expired or began elsewhere. Please try again.',
  ACCESS_TOKEN_DECODE_ERROR: 'That GitHub sign-in expired or began elsewhere. Please try again.',
  ACCESS_TOKEN_ALREADY_EXPIRED: 'That GitHub sign-in expired or began elsewhere. Please try again.',
};

const INVITE: Readonly<Record<SignInStep, string>> = {
  login: 'An invitation is needed to create an account.',
  register: 'That invite code is not valid: it may be mistyped, used, revoked or expired.',
  github:
    'This GitHub account has no account here yet. To sign up, use "Create an account" with your invite code.',
};

/** What to tell the reader when a sign-in request fails. */
export function signInProblem(error: unknown, step: SignInStep): string {
  if (!(error instanceof HttpErrorResponse)) return 'Something went wrong. Please try again.';
  const limited = limitProblem(error);
  if (limited) return limited;
  if (error.status === 0 || error.status >= 500) {
    return 'The server could not be reached. Please try again in a moment.';
  }
  if (step === 'github' && error.status === 404) {
    return 'GitHub sign-in is not set up on this server.';
  }
  if (error.status === 422) return 'Please check the email address.';

  const detail: unknown = error.error?.detail;
  if (detail === 'INVITE_REQUIRED') return INVITE[step];
  if (typeof detail === 'string' && detail in SENTENCES) return SENTENCES[detail];
  // REGISTER_INVALID_PASSWORD carries the server's own sentence: "A password needs at least 12…"
  if (typeof detail === 'object' && detail !== null && 'reason' in detail) {
    return String(detail.reason);
  }
  return 'Something went wrong. Please try again.';
}
