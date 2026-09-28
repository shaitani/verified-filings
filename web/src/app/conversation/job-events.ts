import { Injectable, InjectionToken, inject } from '@angular/core';
import { Observable } from 'rxjs';

import { apiPath } from '../api/api.service';
import type { JobEvent } from '../api/types';

/** Opens an event stream. A token only so tests can stand in for the browser's EventSource. */
export const OPEN_EVENT_SOURCE = new InjectionToken<(url: string) => EventSource>(
  'OPEN_EVENT_SOURCE',
  {
    providedIn: 'root',
    // Same origin, so the session cookie goes with it: no header needed (DESIGN §10).
    factory: () => (url: string) => new EventSource(url),
  },
);

// The server names each frame by its kind ("event: stage"), and a named frame reaches
// only a listener for that name -- never onmessage.
const KINDS: readonly JobEvent['kind'][] = ['stage', 'done', 'failed'];

/** One round's live events, from `GET /api/jobs/{id}/events`. */
@Injectable({ providedIn: 'root' })
export class JobEvents {
  private readonly open = inject(OPEN_EVENT_SOURCE);

  /**
   * Each stage, then `done` or `failed`, then complete. A dropped connection is an error,
   * not a silent reconnect: the caller re-reads the round, which the database has.
   */
  watch(jobId: string): Observable<JobEvent> {
    return new Observable<JobEvent>((subscriber) => {
      const source = this.open(apiPath('/api/jobs/{job_id}/events', { job_id: jobId }));
      for (const kind of KINDS) {
        source.addEventListener(kind, (frame) => {
          const event = JSON.parse((frame as MessageEvent<string>).data) as JobEvent;
          subscriber.next(event);
          if (event.kind !== 'stage') {
            source.close(); // the round is over; the server ends the stream too
            subscriber.complete();
          }
        });
      }
      source.onerror = () => {
        source.close(); // otherwise the browser reconnects on its own, unasked
        subscriber.error(new Error(`the event stream for job ${jobId} dropped`));
      };
      return () => source.close();
    });
  }
}
