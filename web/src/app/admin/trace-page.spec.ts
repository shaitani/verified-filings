import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';

import type { AdminTrace } from '../api/types';
import { TracePage } from './trace-page';

const settle = () => new Promise((resolve) => setTimeout(resolve));

const TRACE: AdminTrace = {
  job: {
    job_id: 'j1',
    conversation_id: 'c1',
    user_email: 'reader@example.com',
    question: "Apple's balances?",
    round: 1,
    status: 'done',
    created_at: '2026-10-01T15:00:00Z',
    finished_at: '2026-10-01T15:00:09Z',
    reply_status: 'partial',
    reports: 1,
    has_trace: true,
  },
  code_version: 'abc123+dirty',
  models: { parser: 'qwen2.5-coder:7b', embedding: 'nomic-embed-text' },
  query_in: { elements: [] },
  plan: null,
  result: null,
  model_calls: [
    {
      stage: 'parse',
      model: 'qwen2.5-coder:7b',
      prompt: 'PROMPT TEXT',
      reply: 'REPLY TEXT',
      seconds: 2.5,
      error: null,
    },
  ],
  statements: [{ sql: 'SELECT 1', verdict: { ok: true }, error: null }],
  timings: { parse: 2.5, map: 1.25 },
  errors: [
    { stage: 'map', type: 'ValueError', message: 'no such concept', traceback: 'Traceback…' },
  ],
  reports: [
    {
      id: 'r1',
      job_id: 'j1',
      user_email: 'reader@example.com',
      question: "Apple's balances?",
      note: 'goodwill looks wrong',
      created_at: '2026-10-01T15:01:00Z',
    },
  ],
};

describe('TracePage', () => {
  let http: HttpTestingController;

  beforeEach(() => {
    TestBed.configureTestingModule({
      imports: [TracePage],
      providers: [provideHttpClient(), provideHttpClientTesting(), provideRouter([])],
    });
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => http.verify());

  async function opened(answer: (req: ReturnType<HttpTestingController['expectOne']>) => void) {
    const fixture = TestBed.createComponent(TracePage);
    fixture.componentRef.setInput('jobId', 'j1');
    fixture.detectChanges();
    await settle();
    answer(http.expectOne('/api/admin/jobs/j1/trace'));
    await settle();
    await fixture.whenStable();
    return fixture.nativeElement as HTMLElement;
  }

  it('lays the round out: header, reports, errors, timings, calls, statements', async () => {
    const page = await opened((req) => req.flush(TRACE));
    const text = page.textContent ?? '';
    expect(page.querySelector('h1')?.textContent).toContain("Apple's balances?");
    expect(text).toContain('reader@example.com');
    expect(page.querySelector('.dirty')).not.toBeNull(); // written from uncommitted code
    expect(page.querySelector('.reports')?.textContent).toContain('goodwill looks wrong');
    expect(page.querySelector('.errors')?.textContent).toContain('no such concept');
    expect(text).toContain('Model calls (1)');
    expect(text).toContain('SQL statements (1)');
    expect(text).toContain('3.8 s'); // 2.5 + 1.25, the round's total
    expect(text).toContain('Opening this is recorded in the audit log.');
  });

  it('says so when there is no such round', async () => {
    const page = await opened((req) =>
      req.flush({ detail: 'NOT_FOUND' }, { status: 404, statusText: 'Not Found' }),
    );
    expect(page.querySelector('[role=alert]')?.textContent).toContain('There is no such round.');
  });

  it('copies the whole trace as JSON', async () => {
    const writeText = vi.fn<(text: string) => Promise<void>>(async () => undefined);
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true });
    const page = await opened((req) => req.flush(TRACE));
    const button = [...page.querySelectorAll('button')].find((b) =>
      b.textContent?.includes('Copy the whole trace as JSON'),
    )!;
    button.click();
    await settle();
    expect(JSON.parse(writeText.mock.calls[0][0])).toEqual(TRACE);
  });
});
