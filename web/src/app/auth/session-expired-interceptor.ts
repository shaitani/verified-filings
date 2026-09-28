import { HttpErrorResponse, HttpInterceptorFn } from '@angular/common/http';
import { inject } from '@angular/core';
import { Router } from '@angular/router';
import { catchError, throwError } from 'rxjs';

import { AuthStore } from './auth-store';
import { signInPage } from './auth-guards';

/**
 * A 401 from a question route means the session ended (it expired, or was signed out
 * elsewhere): forget the user and go to sign in, back to this page afterwards.
 * Not for /api/me or /api/auth/*, whose 401s the sign-in code handles itself.
 */
export const sessionExpiredInterceptor: HttpInterceptorFn = (request, next) => {
  const auth = inject(AuthStore);
  const router = inject(Router);
  return next(request).pipe(
    catchError((error: unknown) => {
      const owned = request.url === '/api/me' || request.url.startsWith('/api/auth/');
      if (error instanceof HttpErrorResponse && error.status === 401 && !owned) {
        auth.forget();
        void router.navigateByUrl(signInPage(router, router.url));
      }
      return throwError(() => error);
    }),
  );
};
