import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';

import type { JobEvent } from '../api/types';
import { ThreadPage } from './thread-page';
import { DONE_REPLY, FakeEventSource, conversation, fakeEventSource, round } from './testing';

describe('ThreadPage', () => {
  let http: HttpTestingController;

  beforeEach(() => {
    FakeEventSource.opened = [];
    TestBed.configureTestingModule({
      imports: [ThreadPage],
      providers: [provideHttpClient(), provideHttpClientTesting(), fakeEventSource],
    });
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => http.verify());

  async function shown(...rounds: Parameters<typeof conversation>) {
    const fixture = TestBed.createComponent(ThreadPage);
    fixture.componentRef.setInput('conversationId', 'c1');
    TestBed.tick(); // runs the effect that opens the conversation
    http.expectOne('/api/conversations/c1').flush(conversation(...rounds));
    await new Promise((resolve) => setTimeout(resolve)); // the store takes in the answer
    await fixture.whenStable();
    const text = () =>
      ((fixture.nativeElement as HTMLElement).textContent ?? '').replace(/\s+/g, ' ');
    return { fixture, text };
  }

  it('shows the question and what a running round is doing', async () => {
    const { fixture, text } = await shown(round({ status: 'queued' }));
    expect(text()).toContain("What was Apple's revenue in fiscal 2024?");
    expect(text()).toContain('Waiting for the question before yours to finish');

    FakeEventSource.last().send({ kind: 'stage', stage: 'fetching', seconds: 7.6 } as JobEvent);
    await fixture.whenStable();
    expect(text()).toContain('Fetching the figures… 8 s');
  });

  it("shows a failed round's sentence, as the server wrote it", async () => {
    const { text } = await shown(
      round({ status: 'failed', message: 'Something went terribly wrong.' }),
    );
    expect(text()).toContain('Something went terribly wrong.');
  });

  it('shows what the reader picked to start a later round', async () => {
    const pick = { ask_id: 'a1', kind: 'option', option_id: 'o2', text: 'Gross margin' } as const;
    const { text } = await shown(
      round({ status: 'done', reply: DONE_REPLY }),
      round({
        round: 2,
        job_id: 'j2',
        status: 'done',
        answers: [pick],
        reply: { ...DONE_REPLY, job_id: 'j2' },
      }),
    );
    expect(text()).toContain('You chose: Gross margin');
  });
});
