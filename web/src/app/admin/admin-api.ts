import { HttpClient, HttpParams } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { Observable } from 'rxjs';

import { apiPath } from '../api/api.service';
import type {
  AdminActionView,
  AdminInvite,
  AdminJob,
  AdminReport,
  AdminTrace,
  AdminUser,
  InviteCreated,
  NewInviteIn,
  PasswordReset,
} from '../api/types';

/** How many rows an admin list shows: the newest this many (DESIGN §13). */
export const ADMIN_LIST_LIMIT = 100;

/**
 * The admin routes (DESIGN §13), one method each: administrators only, 404 to anyone
 * else. Apart from ApiService so they ship in the admin pages' own bundle -- no other
 * reader downloads them.
 */
@Injectable({ providedIn: 'root' })
export class AdminApi {
  private readonly http = inject(HttpClient);

  /** Every account, oldest first. */
  users(): Observable<readonly AdminUser[]> {
    return this.http.get<readonly AdminUser[]>(apiPath('/api/admin/users'));
  }

  /** A new random password, shown once; their sessions end. */
  resetPassword(userId: string): Observable<PasswordReset> {
    const url = apiPath('/api/admin/users/{user_id}/reset-password', { user_id: userId });
    return this.http.post<PasswordReset>(url, null);
  }

  /** The account and everything of theirs, for good. */
  deleteUser(userId: string): Observable<void> {
    return this.http.delete<void>(apiPath('/api/admin/users/{user_id}', { user_id: userId }));
  }

  /** Debug: out at once, until reactivated. */
  deactivate(userId: string): Observable<void> {
    const url = apiPath('/api/admin/users/{user_id}/deactivate', { user_id: userId });
    return this.http.post<void>(url, null);
  }

  /** Debug: able to sign in again. */
  reactivate(userId: string): Observable<void> {
    const url = apiPath('/api/admin/users/{user_id}/reactivate', { user_id: userId });
    return this.http.post<void>(url, null);
  }

  /** Debug: signed out everywhere; they may sign in again. */
  endSessions(userId: string): Observable<void> {
    const url = apiPath('/api/admin/users/{user_id}/end-sessions', { user_id: userId });
    return this.http.post<void>(url, null);
  }

  /** Invites, newest first -- all of them; the page sorts them by status. */
  invites(): Observable<readonly AdminInvite[]> {
    const params = new HttpParams({ fromObject: { limit: 500 } });
    return this.http.get<readonly AdminInvite[]>(apiPath('/api/admin/invites'), { params });
  }

  /** A single-use code, valid `days`; the code is in this answer and nowhere else. */
  createInvite(days: number): Observable<InviteCreated> {
    const body: NewInviteIn = { days };
    return this.http.post<InviteCreated>(apiPath('/api/admin/invites'), body);
  }

  /** An unspent code can no longer be spent. */
  revokeInvite(inviteId: string): Observable<void> {
    const url = apiPath('/api/admin/invites/{invite_id}/revoke', { invite_id: inviteId });
    return this.http.post<void>(url, null);
  }

  /** Recent rounds, newest first -- all, or only the reported or the failed. */
  jobs(filter: { reported?: boolean; failed?: boolean } = {}): Observable<readonly AdminJob[]> {
    const params = new HttpParams({
      fromObject: {
        limit: ADMIN_LIST_LIMIT,
        ...(filter.reported ? { reported: true } : {}),
        ...(filter.failed ? { failed: true } : {}),
      },
    });
    return this.http.get<readonly AdminJob[]>(apiPath('/api/admin/jobs'), { params });
  }

  /** One round's whole record. Opening it is written to the audit log. */
  trace(jobId: string): Observable<AdminTrace> {
    return this.http.get<AdminTrace>(apiPath('/api/admin/jobs/{job_id}/trace', { job_id: jobId }));
  }

  /** Every "report a problem", newest first. */
  reports(): Observable<readonly AdminReport[]> {
    const params = new HttpParams({ fromObject: { limit: ADMIN_LIST_LIMIT } });
    return this.http.get<readonly AdminReport[]>(apiPath('/api/admin/reports'), { params });
  }

  /** The audit log, newest first. */
  actions(): Observable<readonly AdminActionView[]> {
    const params = new HttpParams({ fromObject: { limit: ADMIN_LIST_LIMIT } });
    return this.http.get<readonly AdminActionView[]>(apiPath('/api/admin/actions'), { params });
  }
}
