import { HttpErrorResponse } from '@angular/common/http';
import { computed, inject } from '@angular/core';
import {
  patchState,
  signalStore,
  withComputed,
  withHooks,
  withMethods,
  withState,
} from '@ngrx/signals';
import { Subscription, firstValueFrom } from 'rxjs';

import { ApiService } from '../api/api.service';
import type { ConversationView, JobStatus, StageEvent } from '../api/types';
import { JobEvents } from './job-events';

const FINISHED: readonly JobStatus[] = ['done', 'failed'];

/** How many dropped streams in a row before telling the reader, and the wait before each retry. */
export const MAX_DROPS = 3;
export const RETRY_MS = 1000;

interface ConversationState {
  conversationId: string | null;
  conversation: ConversationView | null; // as GET /api/conversations/{id} last said
  stage: StageEvent | null; // the running round's latest stage, live
  problem: string | null;
}

/**
 * One open conversation. Provided by the thread page, so it lives and dies with it. The
 * database is the record: whenever a round ends, or the stream drops, it is re-read
 * rather than pieced together from events.
 */
export const ConversationStore = signalStore(
  withState<ConversationState>({
    conversationId: null,
    conversation: null,
    stage: null,
    problem: null,
  }),
  withComputed(({ conversation }) => ({
    /** The round still running, if any: only ever the last one. */
    running: computed(() => {
      const last = conversation()?.rounds.at(-1);
      return last && !FINISHED.includes(last.status) ? last : null;
    }),
  })),
  withMethods((store, api = inject(ApiService), events = inject(JobEvents)) => {
    let watching: Subscription | null = null;
    let retry: ReturnType<typeof setTimeout> | null = null;
    let drops = 0;

    function stop(): void {
      watching?.unsubscribe();
      watching = null;
      if (retry !== null) clearTimeout(retry);
      retry = null;
    }

    async function reload(): Promise<void> {
      const id = store.conversationId();
      if (id === null) return;
      try {
        const conversation = await firstValueFrom(api.conversation(id));
        if (store.conversationId() !== id) return; // another conversation opened meanwhile
        patchState(store, { conversation, problem: null });
        follow();
      } catch (error) {
        const gone = error instanceof HttpErrorResponse && error.status === 404;
        patchState(store, {
          problem: gone
            ? 'This conversation does not exist, or is not yours.'
            : 'The server could not be reached. Please reload the page.',
        });
      }
    }

    /** Attach to the running round's stream, if a round is running. */
    function follow(): void {
      stop();
      const round = store.running();
      if (round === null) {
        patchState(store, { stage: null });
        return;
      }
      watching = events.watch(round.job_id).subscribe({
        next: (event) => {
          drops = 0;
          if (event.kind === 'stage') patchState(store, { stage: event });
        },
        complete: () => void reload(), // done or failed: the database has the round now
        error: () => {
          drops += 1;
          if (drops > MAX_DROPS) {
            patchState(store, {
              problem: 'Lost touch with the server while this was running. Please reload.',
            });
            return;
          }
          retry = setTimeout(() => void reload(), RETRY_MS * drops);
        },
      });
    }

    return {
      /** Show a conversation: read it, and follow its running round if it has one. */
      async open(conversationId: string): Promise<void> {
        stop();
        drops = 0;
        patchState(store, { conversationId, conversation: null, stage: null, problem: null });
        await reload();
      },
      stop,
    };
  }),
  withHooks({ onDestroy: (store) => store.stop() }),
);
