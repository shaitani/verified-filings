import { HttpErrorResponse, HttpHeaders } from '@angular/common/http';

import { limitProblem, waitFor } from './limits';

function refused(status: number, detail: string, retryAfter?: string): HttpErrorResponse {
  const headers = retryAfter ? new HttpHeaders({ 'Retry-After': retryAfter }) : undefined;
  return new HttpErrorResponse({ status, error: { detail }, headers });
}

describe('limitProblem', () => {
  it('says how long to wait, from Retry-After', () => {
    expect(limitProblem(refused(429, 'TOO_MANY_ATTEMPTS', '720'))).toBe(
      'Too many sign-in attempts. Please try again in 12 minutes.',
    );
    expect(limitProblem(refused(429, 'DAILY_LIMIT', '7200'))).toContain('ask again in 2 hours');
  });

  it('reads a busy server as busy, not as an outage', () => {
    expect(limitProblem(refused(503, 'SERVER_BUSY', '60'))).toBe(
      'The server is busy with other questions. Please try again in a minute.',
    );
  });

  it('says when questions are waiting, which has no time', () => {
    expect(limitProblem(refused(429, 'TOO_MANY_QUESTIONS'))).toContain('once one of them finishes');
  });

  it('leaves every other refusal to the page', () => {
    expect(limitProblem(refused(400, 'LOGIN_BAD_CREDENTIALS'))).toBeNull();
    expect(limitProblem(new HttpErrorResponse({ status: 503 }))).toBeNull();
    expect(limitProblem(new Error('no'))).toBeNull();
  });
});

describe('waitFor', () => {
  it('rounds up to words', () => {
    expect(waitFor('1')).toBe('a minute');
    expect(waitFor('61')).toBe('2 minutes');
    expect(waitFor('3600')).toBe('an hour');
    expect(waitFor('86400')).toBe('24 hours');
  });

  it('copes with no header or a strange one', () => {
    expect(waitFor(null)).toBe('a little while');
    expect(waitFor('soon')).toBe('a little while');
  });
});
