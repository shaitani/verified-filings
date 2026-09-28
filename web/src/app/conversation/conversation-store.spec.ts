import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';

import type { JobEvent } from '../api/types';
import { ConversationStore, MAX_DROPS, RETRY_MS } from './conversation-store';
import {
  DONE_REPLY,
  FakeEventSource,
  REPLIES,
  conversation,
  fakeEventSource,
  round,
} from './conversation.testing';

const settle = () => new Promise((resolve) => setTimeout(resolve));

describe('ConversationStore', () => {
  let store: InstanceType<typeof ConversationStore>;
  let http: HttpTestingController;

  beforeEach(() => {
    FakeEventSource.opened = [];
    TestBed.configureTestingModule({
      providers: [
        ConversationStore,
        provideHttpClient(),
        provideHttpClientTesting(),
        fakeEventSource,
      ],
    });
    store = TestBed.inject(ConversationStore);
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    vi.useRealTimers();
    http.verify();
  });

  async function opened(...rounds: Parameters<typeof conversation>) {
    const done = store.open('c1');
    http.expectOne('/api/conversations/c1').flush(conversation(...rounds));
    await done;
  }

  it('follows nothing when every round has finished', async () => {
    await opened(round({ status: 'done', reply: DONE_REPLY }));
    expect(store.running()).toBeNull();
    expect(FakeEventSource.opened).toEqual([]);
  });

  it("follows a running round's stages, then re-reads the conversation when it ends", async () => {
    await opened(round({ status: 'parsing' }));
    const source = FakeEventSource.last();
    expect(source.url).toBe('/api/jobs/j1/events');

    source.send({ kind: 'stage', stage: 'fetching', seconds: 4.2 } as JobEvent);
    expect(store.stage()?.stage).toBe('fetching');

    source.send({ kind: 'done', reply: DONE_REPLY } as JobEvent);
    http
      .expectOne('/api/conversations/c1')
      .flush(conversation(round({ status: 'done', reply: DONE_REPLY })));
    await settle();
    expect(store.running()).toBeNull();
    expect(store.conversation()?.rounds[0].reply?.status).toBe('refused');
  });

  it('re-reads and re-attaches when the line drops mid-round', async () => {
    vi.useFakeTimers();
    await opened(round({ status: 'mapping' }));
    FakeEventSource.last().drop();

    await vi.advanceTimersByTimeAsync(RETRY_MS);
    http.expectOne('/api/conversations/c1').flush(conversation(round({ status: 'fetching' })));
    await vi.advanceTimersByTimeAsync(0);
    expect(FakeEventSource.opened).toHaveLength(2); // attached again
  });

  it('tells the reader after the line has dropped too many times', async () => {
    vi.useFakeTimers();
    await opened(round({ status: 'mapping' }));
    for (let drop = 1; drop <= MAX_DROPS; drop++) {
      FakeEventSource.last().drop();
      await vi.advanceTimersByTimeAsync(RETRY_MS * drop);
      http.expectOne('/api/conversations/c1').flush(conversation(round({ status: 'mapping' })));
      await vi.advanceTimersByTimeAsync(0);
    }
    FakeEventSource.last().drop();
    expect(store.problem()).toContain('Lost touch with the server');
  });

  it('sends picks, then follows the round they started', async () => {
    await opened(round({ status: 'done', reply: REPLIES.clarification }));
    const pick = { kind: 'option', ask_id: 'a1', option_id: 'o2' } as const;
    const sent = store.answer([pick]);

    const request = http.expectOne({ method: 'POST', url: '/api/conversations/c1/answers' });
    expect(request.request.body).toEqual({ answers: [pick] });
    request.flush({ conversation_id: 'c1', job_id: 'j2' });
    await settle();
    http
      .expectOne('/api/conversations/c1')
      .flush(
        conversation(
          round({ status: 'done', reply: REPLIES.clarification }),
          round({ round: 2, job_id: 'j2', status: 'queued', answers: [{ ...pick, text: 'x' }] }),
        ),
      );
    await sent;
    expect(store.running()?.job_id).toBe('j2');
    expect(FakeEventSource.last().url).toBe('/api/jobs/j2/events');
    expect(store.answering()).toBe(false);
  });

  it("keeps the server's reason when an answer is refused", async () => {
    await opened(round({ status: 'done', reply: REPLIES.clarification }));
    const sent = store.answer([{ kind: 'option', ask_id: 'a1', option_id: 'o9' }]);
    http
      .expectOne('/api/conversations/c1/answers')
      .flush({ detail: 'UNKNOWN_CHOICE' }, { status: 400, statusText: 'Bad Request' });
    await settle();
    http
      .expectOne('/api/conversations/c1')
      .flush(conversation(round({ status: 'done', reply: REPLIES.clarification })));
    await sent;
    expect(store.problem()).toBe('That choice is no longer on offer. Please reload the page.');
  });

  it('says so when the conversation is not the reader’s', async () => {
    const done = store.open('c9');
    http.expectOne('/api/conversations/c9').flush(null, { status: 404, statusText: 'Not Found' });
    await done;
    expect(store.problem()).toBe('This conversation does not exist, or is not yours.');
  });
});
