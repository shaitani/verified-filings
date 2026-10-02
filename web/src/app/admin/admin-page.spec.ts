import { HttpErrorResponse, provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { MatDialog } from '@angular/material/dialog';
import { of } from 'rxjs';

import type { AdminActionView, AdminInvite, AdminJob, AdminReport, AdminUser } from '../api/types';
import { ConfirmDialog, NewInviteDialog, ShownOnceDialog } from './admin-dialogs';
import { AdminPage } from './admin-page';
import { adminProblem } from './admin-store';

const settle = () => new Promise((resolve) => setTimeout(resolve));

function user(over: Partial<AdminUser>): AdminUser {
  return {
    id: 'u1',
    email: 'reader@example.com',
    created_at: '2026-09-30T10:00:00Z',
    is_active: true,
    is_superuser: false,
    sign_in_providers: [],
    sessions: 1,
    questions_today: 2,
    ...over,
  };
}

function invite(over: Partial<AdminInvite>): AdminInvite {
  return {
    id: 'i1',
    status: 'open',
    created_at: '2026-10-01T10:00:00Z',
    expires_at: '2026-10-15T10:00:00Z',
    used_at: null,
    used_by_email: null,
    revoked_at: null,
    created_by_email: 'boss@example.com',
    ...over,
  };
}

const USERS = [
  user({ id: 'a1', email: 'boss@example.com', is_superuser: true }),
  user({ id: 'u1', email: 'reader@example.com' }),
];
function round(over: Partial<AdminJob>): AdminJob {
  return {
    job_id: 'j1',
    conversation_id: 'c1',
    user_email: 'reader@example.com',
    question: "Apple's balances?",
    round: 1,
    status: 'done',
    created_at: '2026-10-01T15:00:00Z',
    finished_at: '2026-10-01T15:00:09Z',
    reply_status: 'partial',
    reports: 0,
    has_trace: true,
    ...over,
  };
}

const ROUNDS = [
  round({ job_id: 'j1', reports: 1 }),
  round({ job_id: 'j2', status: 'failed', reply_status: null }),
];
const REPORTS: AdminReport[] = [
  {
    id: 'r1',
    job_id: 'j1',
    user_email: 'reader@example.com',
    question: "Apple's balances?",
    note: 'goodwill looks wrong',
    created_at: '2026-10-01T15:01:00Z',
  },
];
const ACTIONS: AdminActionView[] = [
  {
    id: 'x1',
    created_at: '2026-10-01T16:00:00Z',
    admin_email: 'boss@example.com',
    action: 'end_sessions',
    target_id: 'u1',
    target_email: 'reader@example.com',
    detail: { ended: 1 },
  },
  {
    id: 'x2',
    created_at: '2026-10-01T15:30:00Z',
    admin_email: 'boss@example.com',
    action: 'create_invite',
    target_id: 'i1',
    target_email: null,
    detail: { days: 14 },
  },
];

const INVITES = [
  invite({ id: 'i1' }),
  invite({ id: 'i2' }),
  invite({ id: 'i3', status: 'used', used_by_email: 'reader@example.com' }),
  invite({ id: 'i4', status: 'revoked' }),
];

describe('AdminPage', () => {
  let http: HttpTestingController;
  let answers: unknown[]; // what each dialog opened closes with, in order
  let opened: { component: unknown; data: unknown }[];

  beforeEach(() => {
    answers = [];
    opened = [];
    const dialog = {
      open: (component: unknown, config: { data?: unknown } = {}) => {
        opened.push({ component, data: config.data });
        return { afterClosed: () => of(answers.shift()) };
      },
    };
    TestBed.configureTestingModule({
      imports: [AdminPage],
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        provideRouter([]),
        { provide: MatDialog, useValue: dialog },
      ],
    });
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => http.verify());

  function flushLists(users = USERS, invites = INVITES, rounds = ROUNDS) {
    http.expectOne('/api/admin/users').flush(users);
    http.expectOne((r) => r.url === '/api/admin/invites').flush(invites);
    http.expectOne((r) => r.url === '/api/admin/jobs').flush(rounds);
    http.expectOne((r) => r.url === '/api/admin/reports').flush(REPORTS);
    http.expectOne((r) => r.url === '/api/admin/actions').flush(ACTIONS);
  }

  async function openTab(
    fixture: { whenStable(): Promise<unknown> },
    page: HTMLElement,
    index: number,
  ) {
    (page.querySelectorAll('[role=tab]')[index] as HTMLElement).click();
    await settle();
    await fixture.whenStable();
  }

  async function shown() {
    const fixture = TestBed.createComponent(AdminPage);
    fixture.detectChanges();
    flushLists();
    await settle();
    await fixture.whenStable();
    const page = fixture.nativeElement as HTMLElement;
    return {
      fixture,
      page,
      component: fixture.componentInstance as unknown as Record<
        string,
        (...a: unknown[]) => Promise<void>
      >,
    };
  }

  function labels(page: HTMLElement): string[] {
    return [...page.querySelectorAll('[role=tab]')].map((t) => t.textContent?.trim() ?? '');
  }

  it('counts every tab, the invites by status with open first', async () => {
    const { fixture, page } = await shown();
    expect(labels(page)).toEqual(['Users (2)', 'Invites (4)', 'Rounds (2)', 'Audit log (2)']);
    (page.querySelectorAll('[role=tab]')[1] as HTMLElement).click(); // open Invites
    await settle();
    await fixture.whenStable();
    expect(labels(page).slice(4)).toEqual(['Open (2)', 'Used (1)', 'Revoked (1)', 'Expired (0)']);
  });

  it("greys out an administrator's actions, and keeps a reader's", async () => {
    const { page } = await shown();
    const rows = [...page.querySelectorAll('tr[mat-row], tr.mat-mdc-row')];
    const buttons = (row: Element) => [...row.querySelectorAll('button')];
    expect(rows[0].classList).toContain('greyed');
    expect(buttons(rows[0]).every((b) => b.disabled)).toBe(true);
    expect(buttons(rows[1]).some((b) => b.disabled)).toBe(false);
    expect(buttons(rows[1]).map((b) => b.textContent?.trim())).toEqual([
      'Reset password',
      'Delete',
      'debug ▾',
    ]);
  });

  it('shows a reset password once, after a confirmation', async () => {
    const { component } = await shown();
    answers.push(true);
    const done = component['resetPassword'](USERS[1]);
    await settle();
    const reset = http.expectOne('/api/admin/users/u1/reset-password');
    expect(reset.request.method).toBe('POST');
    reset.flush({ password: 'n3w-pa55word-xyz' });
    await settle();
    flushLists();
    await done;
    expect(opened.map((o) => o.component)).toEqual([ConfirmDialog, ShownOnceDialog]);
    expect(opened[1].data).toMatchObject({ value: 'n3w-pa55word-xyz' });
  });

  it('asks for the email to be typed before deleting, and sends nothing if cancelled', async () => {
    const { component } = await shown();
    answers.push(false);
    await component['deleteUser'](USERS[1]);
    expect(opened[0].data).toMatchObject({ typed: 'reader@example.com', danger: true });
    http.expectNone('/api/admin/users/u1');

    answers.push(true);
    const done = component['deleteUser'](USERS[1]);
    await settle();
    const deleted = http.expectOne('/api/admin/users/u1');
    expect(deleted.request.method).toBe('DELETE');
    deleted.flush(null, { status: 204, statusText: 'No Content' });
    await settle();
    flushLists();
    await done;
  });

  it('makes an invite for the days chosen and shows its code once', async () => {
    const { component } = await shown();
    answers.push(30);
    const done = component['newInvite']();
    await settle();
    const created = http.expectOne('/api/admin/invites');
    expect(created.request.body).toEqual({ days: 30 });
    created.flush({ invite: invite({ id: 'i9' }), code: 'K7QM-3XRD-9TPW' });
    await settle();
    flushLists();
    await done;
    expect(opened.map((o) => o.component)).toEqual([NewInviteDialog, ShownOnceDialog]);
    expect(opened[1].data).toMatchObject({ value: 'K7QM-3XRD-9TPW' });
  });

  it('says why when the server refuses, and re-reads the lists', async () => {
    const { fixture, page, component } = await shown();
    answers.push(true);
    const done = component['revokeInvite'](INVITES[0]);
    await settle();
    http
      .expectOne('/api/admin/invites/i1/revoke')
      .flush({ detail: 'INVITE_NOT_OPEN' }, { status: 409, statusText: 'Conflict' });
    await settle();
    flushLists();
    await done;
    await fixture.whenStable();
    expect(page.querySelector('[role=alert]')?.textContent).toContain(
      'already been used or revoked',
    );
  });

  it('links each round to its trace and shows what was reported on it', async () => {
    const { fixture, page } = await shown();
    await openTab(fixture, page, 2);
    const link = page.querySelector<HTMLAnchorElement>('td.question a')!;
    expect(link.getAttribute('href')).toBe('/admin/rounds/j1');
    expect(page.querySelector('.note')?.textContent).toContain('goodwill looks wrong');
    expect(page.querySelector('td.failed')?.textContent?.trim()).toBe('failed');
  });

  it('asks the server for the reported rounds when filtered', async () => {
    const { component } = await shown();
    const done = component['filterRounds']('reported');
    const asked = http.expectOne((r) => r.url === '/api/admin/jobs');
    expect(asked.request.params.get('reported')).toBe('true');
    expect(asked.request.params.has('failed')).toBe(false);
    asked.flush([ROUNDS[0]]);
    await done;
    await settle();
  });

  it('sets the debug actions apart in the audit log', async () => {
    const { fixture, page } = await shown();
    await openTab(fixture, page, 3);
    const cells = [...page.querySelectorAll('td.mat-column-action')];
    expect(cells.map((c) => c.textContent?.trim())).toEqual(['end sessions', 'create invite']);
    expect(cells[0].classList).toContain('debug-action');
    expect(cells[1].classList).not.toContain('debug-action');
  });

  it('says 100+ once a list holds all the server sends', async () => {
    const fixture = TestBed.createComponent(AdminPage);
    fixture.detectChanges();
    const many = Array.from({ length: 100 }, (_, n) => round({ job_id: `j${n}` }));
    flushLists(USERS, INVITES, many);
    await settle();
    await fixture.whenStable();
    expect(labels(fixture.nativeElement as HTMLElement)).toContain('Rounds (100+)');
  });
});

describe('adminProblem', () => {
  const refused = (status: number, detail?: string) =>
    new HttpErrorResponse({ status, error: detail ? { detail } : null });

  it('says each refusal in plain words', () => {
    expect(adminProblem(refused(409, 'ADMIN_PROTECTED'))).toContain("server's command line");
    expect(adminProblem(refused(404, 'NOT_FOUND'))).toContain('no longer exists');
  });

  it('tells a lost admin role from an outage', () => {
    expect(adminProblem(refused(404, 'Not Found'))).toBe('You are no longer an administrator.');
    expect(adminProblem(refused(0))).toContain('could not be reached');
  });
});
