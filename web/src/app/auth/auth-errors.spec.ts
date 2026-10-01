import { HttpErrorResponse, HttpHeaders } from '@angular/common/http';

import { signInProblem } from './auth-errors';

function failed(status: number, detail?: unknown): HttpErrorResponse {
  return new HttpErrorResponse({ status, error: detail === undefined ? null : { detail } });
}

describe('signInProblem', () => {
  it('turns a server code into a sentence', () => {
    expect(signInProblem(failed(400, 'LOGIN_BAD_CREDENTIALS'), 'login')).toBe(
      'That email and password do not match an account.',
    );
  });

  it('says what a missing invitation means for each way in', () => {
    const noInvite = failed(400, 'INVITE_REQUIRED');
    expect(signInProblem(noInvite, 'register')).toContain('does not match this email');
    expect(signInProblem(noInvite, 'github')).toContain('This GitHub account has no invitation');
  });

  it("passes on the server's own password sentence", () => {
    const weak = failed(400, {
      code: 'REGISTER_INVALID_PASSWORD',
      reason: 'A password needs at least 12 characters.',
    });
    expect(signInProblem(weak, 'register')).toBe('A password needs at least 12 characters.');
  });

  it('says how long to wait after too many attempts', () => {
    const limited = new HttpErrorResponse({
      status: 429,
      error: { detail: 'TOO_MANY_ATTEMPTS' },
      headers: new HttpHeaders({ 'Retry-After': '900' }),
    });
    expect(signInProblem(limited, 'login')).toBe(
      'Too many sign-in attempts. Please try again in 15 minutes.',
    );
  });

  it('tells an outage from a refusal', () => {
    expect(signInProblem(failed(0), 'login')).toContain('could not be reached');
    expect(signInProblem(failed(503), 'login')).toContain('could not be reached');
  });

  it('says so when GitHub sign-in is not set up', () => {
    expect(signInProblem(failed(404), 'github')).toBe(
      'GitHub sign-in is not set up on this server.',
    );
  });

  it('never shows a raw code it does not know', () => {
    expect(signInProblem(failed(400, 'SOMETHING_NEW'), 'login')).toBe(
      'Something went wrong. Please try again.',
    );
  });
});
