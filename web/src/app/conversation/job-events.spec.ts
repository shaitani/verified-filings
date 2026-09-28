import { TestBed } from '@angular/core/testing';

import type { JobEvent } from '../api/types';
import { JobEvents } from './job-events';
import { FakeEventSource, fakeEventSource } from './testing';

describe('JobEvents', () => {
  let seen: JobEvent[];
  let ended: 'complete' | 'error' | null;

  beforeEach(() => {
    FakeEventSource.opened = [];
    TestBed.configureTestingModule({ providers: [fakeEventSource] });
    seen = [];
    ended = null;
    TestBed.inject(JobEvents)
      .watch('j1')
      .subscribe({
        next: (event) => seen.push(event),
        complete: () => (ended = 'complete'),
        error: () => (ended = 'error'),
      });
  });

  it("opens the round's own stream", () => {
    expect(FakeEventSource.last().url).toBe('/api/jobs/j1/events');
  });

  it('passes each stage on, then ends with the reply', () => {
    const source = FakeEventSource.last();
    source.send({ kind: 'stage', stage: 'parsing', seconds: 0.2 } as JobEvent);
    source.send({ kind: 'stage', stage: 'mapping', seconds: 3.1 } as JobEvent);
    source.send({ kind: 'done', reply: {} } as JobEvent);
    expect(seen.map((event) => event.kind)).toEqual(['stage', 'stage', 'done']);
    expect([ended, source.closed]).toEqual(['complete', true]);
  });

  it('ends on a failure too', () => {
    FakeEventSource.last().send({ kind: 'failed', message: 'Something went wrong.' } as JobEvent);
    expect(ended).toBe('complete');
  });

  it('reports a dropped line instead of letting the browser reconnect unasked', () => {
    const source = FakeEventSource.last();
    source.drop();
    expect([ended, source.closed]).toEqual(['error', true]);
  });
});
