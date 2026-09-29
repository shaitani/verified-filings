import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';

import type { ConversationSummary } from '../api/types';
import { HistoryList, marker } from './history-list';
import { HistoryStore } from './history-store';

function summary(fields: Partial<ConversationSummary>): ConversationSummary {
  return {
    conversation_id: 'c1',
    question: "What was Apple's revenue in fiscal 2024?",
    created_at: '2026-09-28T12:00:00Z',
    rounds: 1,
    status: 'done',
    reply_status: 'answered',
    ...fields,
  };
}

describe('marker', () => {
  it.each([
    [{ status: 'mapping', reply_status: null }, 'running'],
    [{ status: 'failed', reply_status: null }, 'failed'],
    [{ reply_status: 'answered' }, 'answered'],
    [{ reply_status: 'partial' }, 'partly answered'],
    [{ reply_status: 'refused' }, 'refused'],
  ] as const)('%o reads "%s"', (fields, text) => {
    expect(marker(summary(fields)).text).toBe(text);
  });

  it('singles out a question waiting for the reader', () => {
    expect(marker(summary({ reply_status: 'asked' }))).toMatchObject({
      text: 'waiting for your answer',
      waiting: true,
    });
  });
});

describe('the past questions', () => {
  let http: HttpTestingController;

  beforeEach(() => {
    TestBed.configureTestingModule({
      imports: [HistoryList],
      providers: [provideHttpClient(), provideHttpClientTesting(), provideRouter([])],
    });
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => http.verify());

  it('lists them newest first, each opening its thread', async () => {
    const history = TestBed.inject(HistoryStore);
    const refreshed = history.refresh();
    http
      .expectOne('/api/conversations')
      .flush([
        summary({ conversation_id: 'c2', question: 'Newer?', reply_status: 'asked' }),
        summary({ conversation_id: 'c1', question: 'Older?' }),
      ]);
    await refreshed;

    const fixture = TestBed.createComponent(HistoryList);
    await fixture.whenStable();
    const links = [...(fixture.nativeElement as HTMLElement).querySelectorAll('a')];
    expect(links.map((a) => a.getAttribute('href'))).toEqual(['/c/c2', '/c/c1']);
    expect(links[0].textContent).toContain('waiting for your answer');
    expect(links[0].querySelector('.waiting')).not.toBeNull();
  });

  it('forgets them all on sign-out', async () => {
    const history = TestBed.inject(HistoryStore);
    const refreshed = history.refresh();
    http.expectOne('/api/conversations').flush([summary({})]);
    await refreshed;
    history.clear();
    expect(history.conversations()).toEqual([]);
  });

  it('keeps the old list when a refresh fails, and says so', async () => {
    const history = TestBed.inject(HistoryStore);
    let refreshed = history.refresh();
    http.expectOne('/api/conversations').flush([summary({})]);
    await refreshed;
    refreshed = history.refresh();
    http.expectOne('/api/conversations').flush(null, { status: 503, statusText: 'Unavailable' });
    await refreshed;
    expect(history.conversations()).toHaveLength(1);
    expect(history.problem()).toBe('Past questions could not be loaded.');
  });
});
