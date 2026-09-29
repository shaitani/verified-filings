import { inject } from '@angular/core';
import { patchState, signalStore, withMethods, withState } from '@ngrx/signals';
import { firstValueFrom } from 'rxjs';

import { ApiService } from '../api/api.service';
import type { ConversationSummary } from '../api/types';

interface HistoryState {
  conversations: readonly ConversationSummary[]; // newest first, as the server sends them
  loaded: boolean;
  problem: string | null;
}

/** The signed-in reader's past questions, for the sidebar. */
export const HistoryStore = signalStore(
  { providedIn: 'root' },
  withState<HistoryState>({ conversations: [], loaded: false, problem: null }),
  withMethods((store, api = inject(ApiService)) => ({
    /** Read the list again: after signing in, asking, or a round ending or starting. */
    async refresh(): Promise<void> {
      try {
        const conversations = await firstValueFrom(api.conversations());
        patchState(store, { conversations, loaded: true, problem: null });
      } catch {
        // A 401 is the interceptor's to handle; anything else leaves the old list showing.
        patchState(store, { problem: 'Past questions could not be loaded.' });
      }
    },

    /** Signed out: another reader must never see this list. */
    clear(): void {
      patchState(store, { conversations: [], loaded: false, problem: null });
    },
  })),
);
