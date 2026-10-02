import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import {
  ActivatedRouteSnapshot,
  Router,
  RouterStateSnapshot,
  UrlTree,
  provideRouter,
} from '@angular/router';

import { adminGuard, safeReturnTo, signedInGuard, signedOutGuard } from './auth-guards';

describe('safeReturnTo', () => {
  it.each([
    ['/c/123', '/c/123'],
    ['/', '/'],
    ['https://elsewhere.example', '/'],
    ['//elsewhere.example', '/'],
    ['/\\elsewhere.example', '/'],
    [undefined, '/'],
  ])('%s goes to %s', (asked, safe) => {
    expect(safeReturnTo(asked)).toBe(safe);
  });
});

describe('the guards', () => {
  let http: HttpTestingController;

  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [provideHttpClient(), provideHttpClientTesting(), provideRouter([])],
    });
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => http.verify());

  function run(guard: typeof signedInGuard, url: string, signedIn: boolean, admin = false) {
    const decided = TestBed.runInInjectionContext(() =>
      guard({} as ActivatedRouteSnapshot, { url } as RouterStateSnapshot),
    ) as Promise<boolean | UrlTree>;
    const me = http.expectOne('/api/me');
    if (signedIn) me.flush({ id: 'u1', email: 'me@example.com', is_superuser: admin });
    else me.flush(null, { status: 401, statusText: 'Unauthorized' });
    return decided;
  }

  it('sends a signed-out reader to sign in, remembering the page', async () => {
    const decided = await run(signedInGuard, '/c/123', false);
    expect(TestBed.inject(Router).serializeUrl(decided as UrlTree)).toBe(
      '/login?returnTo=%2Fc%2F123',
    );
  });

  it('lets a signed-in reader through', async () => {
    expect(await run(signedInGuard, '/c/123', true)).toBe(true);
  });

  it('sends a signed-in reader away from the sign-in pages', async () => {
    const decided = await run(signedOutGuard, '/login', true);
    expect(TestBed.inject(Router).serializeUrl(decided as UrlTree)).toBe('/');
  });

  it('lets an administrator into the admin page', async () => {
    expect(await run(adminGuard, '/admin', true, true)).toBe(true);
  });

  it('sends a reader who is not an administrator home', async () => {
    const decided = await run(adminGuard, '/admin', true, false);
    expect(TestBed.inject(Router).serializeUrl(decided as UrlTree)).toBe('/');
  });
});
