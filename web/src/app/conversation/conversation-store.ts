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
import { limitProblem } from '../api/limits';
import { HistoryStore } from '../history/history-store';
import type { AnswerIn, ConversationView, JobStatus, StageEvent } from '../api/types';
import { JobEvents } from './job-events';

const FINISHED: readonly JobStatus[] = ['done', 'failed'];

/** How many dropped streams in a row before telling the reader, and the wait before each retry. */
export const MAX_DROPS = 3;
export const RETRY_MS = 1000;

interface ConversationState {
  conversationId: string | null;
  conversation: ConversationView | null; // as GET /api/conversations/{id} last said
  stage: StageEvent | null; // the running round's latest stage, live
  startedAt: number | null; // when the running round was queued (ms): what the stage clock counts from
  answering: boolean; // picks on their way to the server
  reported: ReadonlySet<string>; // job ids reported this visit: the server keeps no readable copy
  problem: string | null;
}

// Why an answer was refused (app/api/routes.py REFUSALS), in the reader's words.
const ANSWER_REFUSED: Readonly<Record<string, string>> = {
  UNKNOWN_CHOICE: 'That choice is no longer on offer. Please reload the page.',
  ROUND_STILL_RUNNING: 'The last round is still running; its questions are not settled yet.',
  NOTHING_TO_ANSWER: 'There is no question waiting for an answer here.',
  ROUND_CONFLICT: 'Another answer to this conversation arrived first.',
};

/** Why picks could not be sent. */
export function answerProblem(error: unknown): string {
  const limited = limitProblem(error);
  if (limited) return limited;
  if (error instanceof HttpErrorResponse) {
    const detail: unknown = error.error?.detail;
    if (typeof detail === 'string' && detail in ANSWER_REFUSED) return ANSWER_REFUSED[detail];
    if (error.status === 0 || error.status >= 500) {
      return 'The server could not be reached. Please try again in a moment.';
    }
  }
  return 'That answer could not be sent. Please try again.';
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
    startedAt: null,
    answering: false,
    reported: new Set<string>(),
    problem: null,
  }),
  withComputed(({ conversation }) => ({
    /** The round still running, if any: only ever the last one. */
    running: computed(() => {
      const last = conversation()?.rounds.at(-1);
      return last && !FINISHED.includes(last.status) ? last : null;
    }),
  })),
  withMethods(
    (
      store,
      api = inject(ApiService),
      events = inject(JobEvents),
      history = inject(HistoryStore),
    ) => {
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
          patchState(store, { stage: null, startedAt: null });
          return;
        }
        watching = events.watch(round.job_id).subscribe({
          next: (event) => {
            drops = 0;
            if (event.kind === 'stage') {
              // `seconds` counts from the round's queueing: that fixes when it began.
              patchState(store, { stage: event, startedAt: Date.now() - event.seconds * 1000 });
            }
          },
          complete: () => {
            void reload(); // done or failed: the database has the round now
            void history.refresh(); // and the sidebar's marker for it has changed
          },
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
          patchState(store, {
            conversationId,
            conversation: null,
            stage: null,
            startedAt: null,
            problem: null,
          });
          await reload();
          void history.refresh(); // a question just asked joins the list
        },

        /** Answer the last round's questions: the server starts the next round, which is followed. */
        async answer(picks: readonly AnswerIn[]): Promise<void> {
          const id = store.conversationId();
          if (id === null || store.answering()) return;
          patchState(store, { answering: true, problem: null });
          let refused: string | null = null;
          try {
            await firstValueFrom(api.answer(id, picks));
          } catch (error) {
            refused = answerProblem(error);
          }
          await reload(); // the new round, or -- after a refusal -- the conversation as it now is
          patchState(store, { answering: false, ...(refused ? { problem: refused } : {}) });
          void history.refresh();
        },

        /** "Report a problem" on one round: it becomes a flagged job in the owner's trace CLI. */
        async report(jobId: string, note: string | null): Promise<void> {
          await firstValueFrom(api.feedback(jobId, note)); // a failure is the dialog's to show
          patchState(store, { reported: new Set(store.reported()).add(jobId) });
        },
        stop,
      };
    },
  ),
  withHooks({ onDestroy: (store) => store.stop() }),
);
