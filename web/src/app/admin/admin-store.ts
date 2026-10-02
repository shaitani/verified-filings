import { HttpErrorResponse } from '@angular/common/http';
import { computed, inject } from '@angular/core';
import { patchState, signalStore, withComputed, withMethods, withState } from '@ngrx/signals';
import { Observable, firstValueFrom } from 'rxjs';

import { AdminApi } from './admin-api';
import type {
  AdminActionView,
  AdminInvite,
  AdminJob,
  AdminReport,
  AdminUser,
  InviteStatus,
} from '../api/types';

/** Which rounds the Rounds tab lists. */
export type RoundsFilter = 'all' | 'reported' | 'failed';

interface AdminState {
  users: readonly AdminUser[];
  invites: readonly AdminInvite[]; // newest first, as the server sends them
  rounds: readonly AdminJob[]; // newest first, as `roundsFilter` asks
  roundsFilter: RoundsFilter;
  reports: readonly AdminReport[]; // their notes are shown on the reported rounds
  actions: readonly AdminActionView[]; // the audit log, newest first
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
  withState<AdminState>({
    users: [],
    invites: [],
    rounds: [],
    roundsFilter: 'all',
    reports: [],
    actions: [],
    loaded: false,
    problem: null,
  }),
  withComputed(({ invites, reports }) => ({
    /** Each reported round's notes, oldest first, by job id. */
    notesByJob: computed(() => {
      const by = new Map<string, string[]>();
      for (const report of [...reports()].reverse()) {
        if (!report.note) continue;
        by.set(report.job_id, [...(by.get(report.job_id) ?? []), report.note]);
      }
      return by;
    }),
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
  withMethods((store, api = inject(AdminApi)) => {
    async function load(): Promise<void> {
      try {
        const filter = store.roundsFilter();
        const [users, invites, rounds, reports, actions] = await Promise.all([
          firstValueFrom(api.users()),
          firstValueFrom(api.invites()),
          firstValueFrom(
            api.jobs({ reported: filter === 'reported', failed: filter === 'failed' }),
          ),
          firstValueFrom(api.reports()),
          firstValueFrom(api.actions()),
        ]);
        patchState(store, { users, invites, rounds, reports, actions, loaded: true });
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
      /** List other rounds: all, the reported, or the failed. */
      async filterRounds(roundsFilter: RoundsFilter): Promise<void> {
        patchState(store, { roundsFilter, problem: null });
        try {
          const rounds = await firstValueFrom(
            api.jobs({
              reported: roundsFilter === 'reported',
              failed: roundsFilter === 'failed',
            }),
          );
          patchState(store, { rounds });
        } catch (error) {
          patchState(store, { problem: adminProblem(error) });
        }
      },
      /** The new password, to be shown once; null if refused. */
      async resetPassword(user: AdminUser): Promise<string | null> {
        return (await act(api.resetPassword(user.id)))?.password ?? null;
      },
      async deleteUser(user: AdminUser): Promise<void> {
        await act(api.deleteUser(user.id));
      },
      async deactivate(user: AdminUser): Promise<void> {
        await act(api.deactivate(user.id));
      },
      async reactivate(user: AdminUser): Promise<void> {
        await act(api.reactivate(user.id));
      },
      async endSessions(user: AdminUser): Promise<void> {
        await act(api.endSessions(user.id));
      },
      /** The new code, to be shown once; null if refused. */
      async createInvite(days: number): Promise<string | null> {
        return (await act(api.createInvite(days)))?.code ?? null;
      },
      async revokeInvite(invite: AdminInvite): Promise<void> {
        await act(api.revokeInvite(invite.id));
      },
    };
  }),
);
