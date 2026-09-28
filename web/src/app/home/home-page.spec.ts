import { HttpErrorResponse, provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { Router, provideRouter } from '@angular/router';

import { HomePage, askProblem } from './home-page';

const settle = () => new Promise((resolve) => setTimeout(resolve));

describe('HomePage', () => {
  let http: HttpTestingController;
  let navigate: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    TestBed.configureTestingModule({
      imports: [HomePage],
      providers: [provideHttpClient(), provideHttpClientTesting(), provideRouter([])],
    });
    http = TestBed.inject(HttpTestingController);
    navigate = vi.spyOn(TestBed.inject(Router), 'navigate').mockResolvedValue(true);
  });

  afterEach(() => http.verify());

  async function typed(question: string) {
    const fixture = TestBed.createComponent(HomePage);
    await fixture.whenStable();
    const page = fixture.nativeElement as HTMLElement;
    const box = page.querySelector('textarea')!;
    box.value = question;
    box.dispatchEvent(new Event('input'));
    return { fixture, page, box };
  }

  it('asks, then opens the new conversation', async () => {
    const { page } = await typed('  What was Apple’s revenue in fiscal 2024?  ');
    page.querySelector('form')!.dispatchEvent(new Event('submit'));
    const request = http.expectOne({ method: 'POST', url: '/api/conversations' });
    expect(request.request.body).toEqual({ question: 'What was Apple’s revenue in fiscal 2024?' });
    request.flush({ conversation_id: 'c1', job_id: 'j1' });
    await settle();
    expect(navigate).toHaveBeenCalledWith(['/c', 'c1']);
  });

  it('asks on Enter, but Shift+Enter is a new line', async () => {
    const { box } = await typed('Apple revenue?');
    box.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', shiftKey: true }));
    http.expectNone('/api/conversations');
    box.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter' }));
    http.expectOne('/api/conversations');
  });
});

describe('askProblem', () => {
  it("reads the length limit off the server's refusal", () => {
    const tooLong = new HttpErrorResponse({
      status: 422,
      error: { detail: [{ type: 'string_too_long', ctx: { max_length: 2000 } }] },
    });
    expect(askProblem(tooLong)).toBe('A question can be at most 2000 characters.');
  });

  it('tells an outage from a refusal', () => {
    expect(askProblem(new HttpErrorResponse({ status: 0 }))).toContain('could not be reached');
  });
});
