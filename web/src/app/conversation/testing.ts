// Shared by the conversation tests. Not imported by the app.
import type { ConversationView, RoundView } from '../api/types';
import { OPEN_EVENT_SOURCE } from './job-events';

/** Stands in for the browser's EventSource: the test sends frames and drops the line. */
export class FakeEventSource {
  static opened: FakeEventSource[] = [];

  readonly listeners = new Map<string, (frame: MessageEvent<string>) => void>();
  onerror: (() => void) | null = null;
  closed = false;

  constructor(readonly url: string) {
    FakeEventSource.opened.push(this);
  }

  addEventListener(kind: string, listener: (frame: MessageEvent<string>) => void): void {
    this.listeners.set(kind, listener);
  }

  close(): void {
    this.closed = true;
  }

  /** A frame as the server writes it: named by its kind, its JSON as the data. */
  send(event: { kind: string }): void {
    this.listeners.get(event.kind)?.(new MessageEvent(event.kind, { data: JSON.stringify(event) }));
  }

  drop(): void {
    this.onerror?.();
  }

  static last(): FakeEventSource {
    const source = FakeEventSource.opened.at(-1);
    if (!source) throw new Error('no event stream was opened');
    return source;
  }
}

export const fakeEventSource = {
  provide: OPEN_EVENT_SOURCE,
  useValue: (url: string) => new FakeEventSource(url) as unknown as EventSource,
};

/** A round of conversation c1, in the shape GET /api/conversations/{id} sends. */
export function round(fields: Partial<RoundView> & Pick<RoundView, 'status'>): RoundView {
  return {
    round: 1,
    job_id: 'j1',
    answers: [],
    reply: null,
    message: null,
    ...fields,
  };
}

export function conversation(...rounds: RoundView[]): ConversationView {
  return {
    conversation_id: 'c1',
    question: "What was Apple's revenue in fiscal 2024?",
    created_at: '2026-09-28T00:00:00Z',
    rounds,
  };
}

/** A finished round's reply: one part answered, no figures needed at this slice. */
export const DONE_REPLY = {
  conversation_id: 'c1',
  job_id: 'j1',
  blocking: null,
  parts: [{ part_id: 'e1', text: 'revenue', outcome: 'refused', reason: 'r', ask: null }],
  answer: null,
  status: 'refused',
} as const satisfies RoundView['reply'];
