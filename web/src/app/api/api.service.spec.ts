import { HttpParams, provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { Observable, firstValueFrom } from 'rxjs';

import { ApiService, apiPath } from './api.service';

describe('apiPath', () => {
  it('fills and escapes a placeholder', () => {
    expect(apiPath('/api/jobs/{job_id}', { job_id: 'a b/c' })).toBe('/api/jobs/a%20b%2Fc');
  });

  it('refuses a placeholder left empty', () => {
    expect(() => apiPath('/api/jobs/{job_id}')).toThrow('no value for {job_id}');
  });
});

describe('ApiService', () => {
  let api: ApiService;
  let http: HttpTestingController;

  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [provideHttpClient(), provideHttpClientTesting()],
    });
    api = TestBed.inject(ApiService);
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => http.verify()); // every call made exactly the request expected

  it('asks a question', async () => {
    const created = firstValueFrom(api.ask('Apple revenue 2024?'));
    const request = http.expectOne({ method: 'POST', url: '/api/conversations' });
    expect(request.request.body).toEqual({ question: 'Apple revenue 2024?' });
    request.flush({ conversation_id: 'c1', job_id: 'j1' });
    expect(await created).toEqual({ conversation_id: 'c1', job_id: 'j1' });
  });

  it('answers the last round in its conversation', () => {
    const pick = { kind: 'option', ask_id: 'a1', option_id: 'o2' } as const;
    api.answer('c1', [pick]).subscribe();
    const request = http.expectOne({ method: 'POST', url: '/api/conversations/c1/answers' });
    expect(request.request.body).toEqual({ answers: [pick] });
  });

  const reads: [string, (api: ApiService) => Observable<unknown>, string][] = [
    ['conversations', (a) => a.conversations(), '/api/conversations'],
    ['a conversation', (a) => a.conversation('c1'), '/api/conversations/c1'],
    ['a job', (a) => a.job('j1'), '/api/jobs/j1'],
    ['who is signed in', (a) => a.me(), '/api/me'],
  ];

  it.each(reads)('reads %s', (_, call, url) => {
    call(api).subscribe();
    http.expectOne({ method: 'GET', url });
  });

  it('reports a problem, with or without a note', () => {
    api.feedback('j1', 'goodwill is wrong').subscribe();
    api.feedback('j2', null).subscribe();
    const [first, second] = [
      http.expectOne('/api/jobs/j1/feedback'),
      http.expectOne('/api/jobs/j2/feedback'),
    ];
    expect([first.request.body, second.request.body]).toEqual([
      { note: 'goodwill is wrong' },
      { note: null },
    ]);
  });

  it('signs in with a form whose username is the email', () => {
    api.login('me@example.com', 'secret').subscribe();
    const request = http.expectOne({ method: 'POST', url: '/api/auth/login' });
    const form = request.request.body as HttpParams;
    expect([form.get('username'), form.get('password')]).toEqual(['me@example.com', 'secret']);
  });

  it('registers with only an email, a password and the invite code', () => {
    api.register('me@example.com', 'secret', 'K7QM-3XRD-9TPW').subscribe();
    const request = http.expectOne({ method: 'POST', url: '/api/auth/register' });
    expect(request.request.body).toEqual({
      email: 'me@example.com',
      password: 'secret',
      invite_code: 'K7QM-3XRD-9TPW',
    });
  });

  it('signs out', () => {
    api.logout().subscribe();
    http.expectOne({ method: 'POST', url: '/api/auth/logout' });
  });

  it("hands back GitHub's sign-in address", async () => {
    const url = firstValueFrom(api.gitHubSignInUrl());
    http
      .expectOne({ method: 'GET', url: '/api/auth/github/authorize' })
      .flush({ authorization_url: 'https://github.com/login/oauth/authorize?x=1' });
    expect(await url).toBe('https://github.com/login/oauth/authorize?x=1');
  });
});
