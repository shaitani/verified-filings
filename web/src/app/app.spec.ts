import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';

import { App } from './app';

describe('App', () => {
  let http: HttpTestingController;

  beforeEach(() => {
    TestBed.configureTestingModule({
      imports: [App],
      providers: [provideHttpClient(), provideHttpClientTesting()],
    });
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => http.verify()); // no request left unanswered

  async function rendered(
    answer: (request: ReturnType<HttpTestingController['expectOne']>) => void,
  ) {
    const fixture = TestBed.createComponent(App);
    TestBed.tick(); // runs the resource, which sends its request
    answer(http.expectOne('/api/health'));
    await fixture.whenStable();
    const text = (fixture.nativeElement as HTMLElement).textContent ?? '';
    return text.replace(/\s+/g, ' '); // the template's line breaks are not content
  }

  it('shows the Web Server as reachable', async () => {
    const text = await rendered((request) => request.flush({ status: 'ok' }));
    expect(text).toContain('Web Server: ok');
  });

  it('says so when the Web Server cannot be reached', async () => {
    const text = await rendered((request) =>
      request.flush({ status: 'database unreachable' }, { status: 503, statusText: 'Unavailable' }),
    );
    expect(text).toContain('Web Server: unreachable');
  });
});
