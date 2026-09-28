import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';

import { AuthStore } from './auth-store';

const ME = {
  id: 'u1',
  email: 'me@example.com',
  is_active: true,
  is_superuser: false,
  is_verified: false,
};

/** Let the store's awaits move on to their next request. */
const settle = () => new Promise((resolve) => setTimeout(resolve));

describe('AuthStore', () => {
  let auth: InstanceType<typeof AuthStore>;
  let http: HttpTestingController;

  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [provideHttpClient(), provideHttpClientTesting()],
    });
    auth = TestBed.inject(AuthStore);
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => http.verify());

  it('asks the server once, however many ask it', async () => {
    const [first, second] = [auth.check(), auth.check()];
    http.expectOne('/api/me').flush(ME);
    await Promise.all([first, second]);
    expect(auth.signedIn()).toBe(true);
    expect(auth.user()?.email).toBe('me@example.com');
  });

  it('reads a 401 as nobody signed in', async () => {
    const checked = auth.check();
    http.expectOne('/api/me').flush(null, { status: 401, statusText: 'Unauthorized' });
    await checked;
    expect([auth.checked(), auth.signedIn()]).toEqual([true, false]);
  });

  it('signs in, then asks who is signed in', async () => {
    const done = auth.signIn('me@example.com', 'secret');
    http.expectOne('/api/auth/login').flush(null, { status: 204, statusText: 'No Content' });
    await settle();
    http.expectOne('/api/me').flush(ME);
    await done;
    expect(auth.signedIn()).toBe(true);
  });

  it('registers, then signs in: registering alone does not', async () => {
    const done = auth.register('me@example.com', 'secret', 'K7QM-3XRD-9TPW');
    http.expectOne('/api/auth/register').flush(ME, { status: 201, statusText: 'Created' });
    await settle();
    http.expectOne('/api/auth/login').flush(null, { status: 204, statusText: 'No Content' });
    await settle();
    http.expectOne('/api/me').flush(ME);
    await done;
    expect(auth.signedIn()).toBe(true);
  });

  it('is signed out afterwards even when the server cannot be reached', async () => {
    const checked = auth.check();
    http.expectOne('/api/me').flush(ME);
    await checked;

    const out = auth.signOut();
    http.expectOne('/api/auth/logout').error(new ProgressEvent('offline'));
    await expect(out).rejects.toBeTruthy();
    expect(auth.signedIn()).toBe(false);
  });
});
