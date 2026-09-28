import { HttpClient, provideHttpClient, withInterceptors } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { Router, provideRouter } from '@angular/router';

import { AuthStore } from './auth-store';
import { sessionExpiredInterceptor } from './session-expired-interceptor';

describe('sessionExpiredInterceptor', () => {
  let http: HttpTestingController;
  let navigate: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [
        provideHttpClient(withInterceptors([sessionExpiredInterceptor])),
        provideHttpClientTesting(),
        provideRouter([]),
      ],
    });
    http = TestBed.inject(HttpTestingController);
    navigate = vi.spyOn(TestBed.inject(Router), 'navigateByUrl').mockResolvedValue(true);
  });

  afterEach(() => http.verify());

  function fail(url: string) {
    TestBed.inject(HttpClient)
      .get(url)
      .subscribe({ error: () => undefined });
    http.expectOne(url).flush(null, { status: 401, statusText: 'Unauthorized' });
  }

  it('sends the reader to sign in when a question route says the session ended', () => {
    const forget = vi.spyOn(TestBed.inject(AuthStore), 'forget');
    fail('/api/conversations');
    expect(forget).toHaveBeenCalled();
    expect(navigate).toHaveBeenCalled();
  });

  it.each(['/api/me', '/api/auth/login'])('leaves the 401 from %s to the sign-in code', (url) => {
    fail(url);
    expect(navigate).not.toHaveBeenCalled();
  });
});
