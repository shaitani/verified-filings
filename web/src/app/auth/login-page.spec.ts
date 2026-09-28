import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { Router, provideRouter } from '@angular/router';

import { LoginPage } from './login-page';

const settle = () => new Promise((resolve) => setTimeout(resolve));

describe('LoginPage', () => {
  let http: HttpTestingController;
  let navigate: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    TestBed.configureTestingModule({
      imports: [LoginPage],
      providers: [provideHttpClient(), provideHttpClientTesting(), provideRouter([])],
    });
    http = TestBed.inject(HttpTestingController);
    navigate = vi.spyOn(TestBed.inject(Router), 'navigateByUrl').mockResolvedValue(true);
  });

  afterEach(() => http.verify());

  async function submitted(returnTo?: string) {
    const fixture = TestBed.createComponent(LoginPage);
    if (returnTo) fixture.componentRef.setInput('returnTo', returnTo);
    const page = fixture.nativeElement as HTMLElement;
    await fixture.whenStable();
    for (const [name, value] of [
      ['email', 'me@example.com'],
      ['password', 'secret'],
    ]) {
      const input = page.querySelector<HTMLInputElement>(`input[formcontrolname=${name}]`)!;
      input.value = value;
      input.dispatchEvent(new Event('input'));
    }
    page.querySelector('form')!.dispatchEvent(new Event('submit'));
    return { fixture, page };
  }

  it('signs in and goes back where the reader was going', async () => {
    await submitted('/c/123');
    http.expectOne('/api/auth/login').flush(null, { status: 204, statusText: 'No Content' });
    await settle();
    http.expectOne('/api/me').flush({ id: 'u1', email: 'me@example.com' });
    await settle();
    expect(navigate).toHaveBeenCalledWith('/c/123');
  });

  it('never follows a returnTo off this site', async () => {
    await submitted('https://elsewhere.example');
    http.expectOne('/api/auth/login').flush(null, { status: 204, statusText: 'No Content' });
    await settle();
    http.expectOne('/api/me').flush({ id: 'u1', email: 'me@example.com' });
    await settle();
    expect(navigate).toHaveBeenCalledWith('/');
  });

  it('says why when the server refuses', async () => {
    const { fixture, page } = await submitted();
    http
      .expectOne('/api/auth/login')
      .flush({ detail: 'LOGIN_BAD_CREDENTIALS' }, { status: 400, statusText: 'Bad Request' });
    await settle();
    await fixture.whenStable();
    expect(page.querySelector('[role=alert]')?.textContent).toContain(
      'That email and password do not match an account.',
    );
    expect(navigate).not.toHaveBeenCalled();
  });
});
