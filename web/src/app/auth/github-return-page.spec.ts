import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { ActivatedRoute, Router, provideRouter } from '@angular/router';

import { GitHubReturnPage } from './github-return-page';

const settle = () => new Promise((resolve) => setTimeout(resolve));

describe('GitHubReturnPage', () => {
  let http: HttpTestingController;
  let navigate: ReturnType<typeof vi.spyOn>;

  function arrive(query: Record<string, string>) {
    TestBed.configureTestingModule({
      imports: [GitHubReturnPage],
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        provideRouter([]),
        { provide: ActivatedRoute, useValue: { snapshot: { queryParams: query } } },
      ],
    });
    http = TestBed.inject(HttpTestingController);
    navigate = vi.spyOn(TestBed.inject(Router), 'navigateByUrl').mockResolvedValue(true);
    const fixture = TestBed.createComponent(GitHubReturnPage);
    fixture.detectChanges(); // runs ngOnInit
    return fixture;
  }

  afterEach(() => http.verify());

  it("hands GitHub's code and state to the server, then goes home", async () => {
    arrive({ code: 'c0de', state: 'st4te' });
    const request = http.expectOne((r) => r.url === '/api/auth/github/callback');
    expect(request.request.params.get('code')).toBe('c0de');
    expect(request.request.params.get('state')).toBe('st4te');
    request.flush(null, { status: 204, statusText: 'No Content' });
    await settle();
    http.expectOne('/api/me').flush({ id: 'u1', email: 'me@example.com' });
    await settle();
    expect(navigate).toHaveBeenCalledWith('/', { replaceUrl: true });
  });

  it('says how to sign up when this GitHub account has no account yet', async () => {
    const fixture = arrive({ code: 'c0de', state: 'st4te' });
    http
      .expectOne((r) => r.url === '/api/auth/github/callback')
      .flush({ detail: 'INVITE_REQUIRED' }, { status: 400, statusText: 'Bad Request' });
    await settle();
    await fixture.whenStable();
    const page = fixture.nativeElement as HTMLElement;
    expect(page.querySelector('[role=alert]')?.textContent).toContain('has no account here yet');
  });

  it('asks the server nothing when the reader cancelled on GitHub', async () => {
    const fixture = arrive({ error: 'access_denied' });
    await fixture.whenStable();
    expect((fixture.nativeElement as HTMLElement).textContent).toContain('cancelled');
  });
});
