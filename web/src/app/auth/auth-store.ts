import { HttpErrorResponse } from '@angular/common/http';
import { computed, inject } from '@angular/core';
import { patchState, signalStore, withComputed, withMethods, withState } from '@ngrx/signals';
import { firstValueFrom } from 'rxjs';

import { ApiService } from '../api/api.service';
import type { UserRead } from '../api/types';

interface AuthState {
  user: UserRead | null; // as /api/me last said
  checked: boolean; // false until /api/me has answered once: guards wait for it
}

/** Who is signed in. The session itself is an httpOnly cookie the page never sees. */
export const AuthStore = signalStore(
  { providedIn: 'root' },
  withState<AuthState>({ user: null, checked: false }),
  withComputed(({ user }) => ({
    signedIn: computed(() => user() !== null),
  })),
  withMethods((store, api = inject(ApiService)) => {
    let checking: Promise<void> | null = null; // one /api/me at a time, however many ask

    async function load(): Promise<void> {
      try {
        patchState(store, { user: await firstValueFrom(api.me()), checked: true });
      } catch (error) {
        // 401 is the ordinary answer when nobody is signed in; anything else is an
        // outage, which reads the same here: the sign-in page reports the server.
        if (!(error instanceof HttpErrorResponse && error.status === 401)) console.error(error);
        patchState(store, { user: null, checked: true });
      }
    }

    /** Ask again: after signing in, or when the server says the session ended. */
    function refresh(): Promise<void> {
      checking = load();
      return checking;
    }

    /** The session is over (a 401 elsewhere): drop the user without asking the server. */
    function forget(): void {
      checking = Promise.resolve();
      patchState(store, { user: null, checked: true });
    }

    async function signIn(email: string, password: string): Promise<void> {
      await firstValueFrom(api.login(email, password));
      await refresh();
    }

    return {
      /** Ask the server who is signed in, once; later calls wait on the same answer. */
      check(): Promise<void> {
        checking ??= load();
        return checking;
      },
      refresh,
      forget,
      signIn,

      async register(email: string, password: string, inviteCode: string): Promise<void> {
        await firstValueFrom(api.register(email, password, inviteCode));
        await signIn(email, password); // registering does not sign in by itself
      },

      async finishGitHub(fromGitHub: Readonly<Record<string, string>>): Promise<void> {
        await firstValueFrom(api.gitHubSignIn(fromGitHub));
        await refresh();
      },

      async signOut(): Promise<void> {
        try {
          await firstValueFrom(api.logout());
        } finally {
          forget(); // signed out here even if the server was unreachable
        }
      },
    };
  }),
);
