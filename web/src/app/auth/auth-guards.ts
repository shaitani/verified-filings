import { inject } from '@angular/core';
import { CanActivateFn, Router, UrlTree } from '@angular/router';

import { AuthStore } from './auth-store';

/**
 * Where to go after signing in. Only a path on this site: a crafted
 * `/login?returnTo=https://elsewhere` (or `//elsewhere`) must not send anyone off it.
 */
export function safeReturnTo(returnTo: unknown): string {
  return typeof returnTo === 'string' && /^\/(?![/\\])/.test(returnTo) ? returnTo : '/';
}

/** The sign-in page, remembering where the reader was going. */
export function signInPage(router: Router, returnTo: string): UrlTree {
  const query = returnTo === '/' ? {} : { returnTo };
  return router.createUrlTree(['/login'], { queryParams: query });
}

// Both inject before their first await: inject() works only until a guard yields.

/** Pages for a signed-in reader: anyone else goes to sign in first. */
export const signedInGuard: CanActivateFn = async (_, state) => {
  const [auth, router] = [inject(AuthStore), inject(Router)];
  await auth.check();
  return auth.signedIn() || signInPage(router, state.url);
};

/** The sign-in pages: a reader already signed in has nothing to do there. */
export const signedOutGuard: CanActivateFn = async () => {
  const [auth, router] = [inject(AuthStore), inject(Router)];
  await auth.check();
  return !auth.signedIn() || router.createUrlTree(['/']);
};

/**
 * The admin pages: administrators only; anyone else goes home. A convenience, not the
 * control -- the server answers every admin route with 404 to anyone else (DESIGN §13).
 */
export const adminGuard: CanActivateFn = async (_, state) => {
  const [auth, router] = [inject(AuthStore), inject(Router)];
  await auth.check();
  if (!auth.signedIn()) return signInPage(router, state.url);
  return auth.user()?.is_superuser === true || router.createUrlTree(['/']);
};
