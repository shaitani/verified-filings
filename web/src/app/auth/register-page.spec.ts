import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';

import { RegisterPage } from './register-page';

const settle = () => new Promise((resolve) => setTimeout(resolve));

describe('RegisterPage, signing up with GitHub', () => {
  let http: HttpTestingController;

  beforeEach(() => {
    TestBed.configureTestingModule({
      imports: [RegisterPage],
      providers: [provideHttpClient(), provideHttpClientTesting(), provideRouter([])],
    });
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => http.verify());

  async function withGitHub(inviteCode: string) {
    const fixture = TestBed.createComponent(RegisterPage);
    const page = fixture.nativeElement as HTMLElement;
    await fixture.whenStable();
    const input = page.querySelector<HTMLInputElement>('input[formcontrolname=inviteCode]')!;
    input.value = inviteCode;
    input.dispatchEvent(new Event('input'));
    page.querySelector<HTMLButtonElement>('button.github')!.click();
    return { fixture, page };
  }

  async function shown(fixture: { whenStable(): Promise<unknown> }, page: HTMLElement) {
    await settle();
    await fixture.whenStable();
    return page.querySelector('[role=alert]')?.textContent ?? '';
  }

  it('asks for the invite code first, and sends nothing without one', async () => {
    const { fixture, page } = await withGitHub('  ');
    expect(await shown(fixture, page)).toContain('Enter your invite code first');
    http.expectNone('/api/auth/github/invite');
  });

  it('hands the code over before leaving for GitHub', async () => {
    const { fixture, page } = await withGitHub(' K7QM-3XRD-9TPW ');
    const held = http.expectOne('/api/auth/github/invite');
    expect(held.request.method).toBe('POST');
    expect(held.request.body).toEqual({ invite_code: 'K7QM-3XRD-9TPW' });
    held.flush(null, { status: 204, statusText: 'No Content' });
    await settle();
    // Answered with an error so the test does not navigate away to GitHub.
    http
      .expectOne('/api/auth/github/authorize')
      .flush(null, { status: 404, statusText: 'Not Found' });
    expect(await shown(fixture, page)).toBe('GitHub sign-in is not set up on this server.');
  });

  it('says a wrong code now, and never goes to GitHub with it', async () => {
    const { fixture, page } = await withGitHub('AAAA-BBBB-CCCC');
    http
      .expectOne('/api/auth/github/invite')
      .flush({ detail: 'INVITE_REQUIRED' }, { status: 400, statusText: 'Bad Request' });
    expect(await shown(fixture, page)).toContain('That invite code is not valid');
    http.expectNone('/api/auth/github/authorize');
  });
});
