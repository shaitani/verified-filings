import { BreakpointObserver } from '@angular/cdk/layout';
import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { of } from 'rxjs';

import { App } from './app';
import { AuthStore } from './auth/auth-store';

describe('App', () => {
  let http: HttpTestingController;

  beforeEach(() => {
    TestBed.configureTestingModule({
      imports: [App],
      providers: [provideHttpClient(), provideHttpClientTesting(), provideRouter([])],
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

  it('offers no sign-out to nobody', async () => {
    const text = await rendered((request) => request.flush({ status: 'ok' }));
    expect(text).not.toContain('Sign out');
  });
});

describe('App on a phone', () => {
  let http: HttpTestingController;

  beforeEach(() => {
    const phone = { observe: () => of({ matches: true, breakpoints: {} }) }; // every query matches
    TestBed.configureTestingModule({
      imports: [App],
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        provideRouter([]),
        { provide: BreakpointObserver, useValue: phone },
      ],
    });
    http = TestBed.inject(HttpTestingController);
  });

  async function signedIn(isSuperuser: boolean) {
    const fixture = TestBed.createComponent(App);
    TestBed.tick();
    http.expectOne('/api/health').flush({ status: 'ok' });
    const refreshed = TestBed.inject(AuthStore).refresh();
    http
      .expectOne('/api/me')
      .flush({ id: 'u1', email: 'me@example.com', is_superuser: isSuperuser });
    await refreshed;
    await fixture.whenStable();
    http.match('/api/conversations').forEach((r) => r.flush([])); // the past questions
    await fixture.whenStable();
    return { fixture, page: fixture.nativeElement as HTMLElement };
  }

  it('shows icons, and keeps the email out of the banner', async () => {
    const { page } = await signedIn(true);
    const labels = [...page.querySelectorAll('mat-toolbar [aria-label]')].map((e) =>
      e.getAttribute('aria-label'),
    );
    expect(labels).toEqual(expect.arrayContaining(['New question', 'Admin', 'Account']));
    expect(page.querySelector('mat-toolbar .who')).toBeNull();
    expect(page.querySelector('mat-toolbar')?.textContent).not.toContain('me@example.com');
  });

  it('shows no Admin icon to a reader who is not one', async () => {
    const { page } = await signedIn(false);
    expect(page.querySelector('mat-toolbar [aria-label=Admin]')).toBeNull();
  });

  it('opens the account panel, and closes it on Escape or a click elsewhere', async () => {
    const { fixture, page } = await signedIn(false);
    const account = page.querySelector<HTMLButtonElement>('[aria-label=Account][aria-haspopup]')!;
    account.click();
    await fixture.whenStable();
    expect(page.querySelector('.account-panel')?.textContent).toContain('me@example.com');
    expect(page.querySelector('.account-panel')?.textContent).toContain('Sign out');

    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
    await fixture.whenStable();
    expect(page.querySelector('.account-panel')).toBeNull();

    account.click();
    await fixture.whenStable();
    document.body.click();
    await fixture.whenStable();
    expect(page.querySelector('.account-panel')).toBeNull();
  });

  it('moves the theme toggle into the account panel, making room for the name', async () => {
    const { fixture, page } = await signedIn(false);
    expect(page.querySelector('mat-toolbar .theme-toggle')).toBeNull();
    expect(page.querySelector('mat-toolbar .word')?.textContent).toContain('Verified Filings');
    page.querySelector<HTMLButtonElement>('[aria-label=Account][aria-haspopup]')!.click();
    await fixture.whenStable();
    const toggle = page.querySelector<HTMLButtonElement>('.account-panel .theme-item')!;
    // The test DOM has no matchMedia; "reduced motion" makes the switch instant.
    vi.stubGlobal('matchMedia', () => ({ matches: true }));
    const wasDark = document.body.classList.contains('dark');
    toggle.click();
    await fixture.whenStable();
    expect(document.body.classList.contains('dark')).toBe(!wasDark);
    expect(page.querySelector('.account-panel')).not.toBeNull(); // a click inside keeps it open
    toggle.click(); // leave the theme as it was
    await fixture.whenStable();
    vi.unstubAllGlobals();
  });
});
