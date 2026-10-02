import { HttpErrorResponse } from '@angular/common/http';
import { computed, inject } from '@angular/core';
import { patchState, signalStore, withComputed, withMethods, withState } from '@ngrx/signals';
import { Observable, firstValueFrom } from 'rxjs';

import { ApiService } from '../api/api.service';
import type { AdminInvite, AdminUser, InviteStatus } from '../api/types';

interface AdminState {
  users: readonly AdminUser[];
  invites: readonly AdminInvite[]; // newest first, as the server sends them
  loaded: boolean;
  problem: string | null;
}

// Why an admin action was refused (app/api/admin_routes.py REFUSALS), in plain words.
const REFUSED: Readonly<Record<string, string>> = {
  ADMIN_PROTECTED: "Administrators can only be changed from the server's command line.",
  INVITE_NOT_OPEN: 'That invite has already been used or revoked.',
  NOT_FOUND: 'That no longer exists; the lists have been refreshed.',
};

/** What to say when an admin call fails. */
export function adminProblem(error: unknown): string {
  if (error instanceof HttpErrorResponse) {
    const detail: unknown = error.error?.detail;
    if (typeof detail === 'string' && detail in REFUSED) return REFUSED[detail];
    // FastAPI's own body for a missing route: what a non-administrator is told.
    if (error.status === 404) return 'You are no longer an administrator.';
    if (error.status === 0 || error.status >= 500) {
      return 'The server could not be reached. Please try again in a moment.';
    }
  }
  return 'That did not work. Please try again.';
}

/** The order the invite sub-tabs come in. */
export const INVITE_TABS: readonly InviteStatus[] = ['open', 'used', 'revoked', 'expired'];

/**
 * The admin page's lists and actions. Provided by the page, so it lives and dies with it.
 * The server is the record: after every action, successful or not, both lists are
 * re-read rather than patched by hand.
 */
export const AdminStore = signalStore(
  withState<AdminState>({ users: [], invites: [], loaded: false, problem: null }),
  withComputed(({ invites }) => ({
    invitesByStatus: computed(() => {
      const by: Record<InviteStatus, AdminInvite[]> = {
        open: [],
        used: [],
        revoked: [],
        expired: [],
      };
      for (const invite of invites()) by[invite.status].push(invite);
      return by;
    }),
  })),
  withMethods((store, api = inject(ApiService)) => {
    async function load(): Promise<void> {
      try {
        const [users, invites] = await Promise.all([
          firstValueFrom(api.adminUsers()),
          firstValueFrom(api.adminInvites()),
        ]);
        patchState(store, { users, invites, loaded: true });
      } catch (error) {
        patchState(store, { problem: adminProblem(error), loaded: true });
      }
    }

    /** One action, then both lists again. Its result, or null with the problem shown. */
    async function act<T>(call: Observable<T>): Promise<T | null> {
      patchState(store, { problem: null });
      try {
        return await firstValueFrom(call, { defaultValue: null as T });
      } catch (error) {
        patchState(store, { problem: adminProblem(error) });
        return null;
      } finally {
        await load();
      }
    }

    return {
      load,
      /** The new password, to be shown once; null if refused. */
      async resetPassword(user: AdminUser): Promise<string | null> {
        return (await act(api.adminResetPassword(user.id)))?.password ?? null;
      },
      async deleteUser(user: AdminUser): Promise<void> {
        await act(api.adminDeleteUser(user.id));
      },
      async deactivate(user: AdminUser): Promise<void> {
        await act(api.adminDeactivate(user.id));
      },
      async reactivate(user: AdminUser): Promise<void> {
        await act(api.adminReactivate(user.id));
      },
      async endSessions(user: AdminUser): Promise<void> {
        await act(api.adminEndSessions(user.id));
      },
      /** The new code, to be shown once; null if refused. */
      async createInvite(days: number): Promise<string | null> {
        return (await act(api.adminCreateInvite(days)))?.code ?? null;
      },
      async revokeInvite(invite: AdminInvite): Promise<void> {
        await act(api.adminRevokeInvite(invite.id));
      },
    };
  }),
);
