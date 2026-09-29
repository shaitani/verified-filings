import { HttpClient, HttpParams } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { Observable, map } from 'rxjs';

import type { paths } from './openapi';
import type {
  AnswerIn,
  AnswersIn,
  ConversationSummary,
  ConversationView,
  FeedbackIn,
  GitHubAuthorize,
  JobCreated,
  JobView,
  NewConversationIn,
  UserCreate,
  UserRead,
} from './types';

/** A route of the Web Server, exactly as the contract names it. */
export type ApiPath = keyof paths;

/**
 * A URL for a contract route, its `{placeholders}` filled. Taking only `ApiPath` means a
 * route renamed or removed on the server stops this compiling, instead of 404ing at runtime.
 */
export function apiPath(path: ApiPath, params: Readonly<Record<string, string>> = {}): string {
  return path.replace(/\{(\w+)\}/g, (_, name: string) => {
    const value = params[name];
    if (value === undefined) throw new Error(`${path}: no value for {${name}}`);
    return encodeURIComponent(value);
  });
}

/**
 * Every call the client makes, one method per route. The event stream is not here: it is
 * an `EventSource`, not an HTTP call.
 */
@Injectable({ providedIn: 'root' })
export class ApiService {
  private readonly http = inject(HttpClient);

  // --- questions (DESIGN §4) -----------------------------------------------------------

  /** A new question: its conversation and first round, queued. */
  ask(question: string): Observable<JobCreated> {
    const body: NewConversationIn = { question };
    return this.http.post<JobCreated>(apiPath('/api/conversations'), body);
  }

  /** Picks for the last round's questions: the next round, queued. */
  answer(conversationId: string, answers: readonly AnswerIn[]): Observable<JobCreated> {
    const body: AnswersIn = { answers };
    const url = apiPath('/api/conversations/{conversation_id}/answers', {
      conversation_id: conversationId,
    });
    return this.http.post<JobCreated>(url, body);
  }

  /** The reader's past questions, newest first. */
  conversations(): Observable<readonly ConversationSummary[]> {
    return this.http.get<readonly ConversationSummary[]>(apiPath('/api/conversations'));
  }

  /** A past conversation reopened: every round, in order. */
  conversation(conversationId: string): Observable<ConversationView> {
    const url = apiPath('/api/conversations/{conversation_id}', {
      conversation_id: conversationId,
    });
    return this.http.get<ConversationView>(url);
  }

  /** One round as it stands -- for a reload or a dropped stream. */
  job(jobId: string): Observable<JobView> {
    return this.http.get<JobView>(apiPath('/api/jobs/{job_id}', { job_id: jobId }));
  }

  /** "Report a problem" on one round (DESIGN §8). */
  feedback(jobId: string, note: string | null): Observable<void> {
    const body: FeedbackIn = { note };
    return this.http.post<void>(apiPath('/api/jobs/{job_id}/feedback', { job_id: jobId }), body);
  }

  // --- sign-in (DESIGN §10) ------------------------------------------------------------

  /** Who is signed in; 401 when nobody is. */
  me(): Observable<UserRead> {
    return this.http.get<UserRead>(apiPath('/api/me'));
  }

  /** Email and password. The server sets the session cookie; nothing comes back. */
  login(email: string, password: string): Observable<void> {
    // FastAPI Users takes a form here, not JSON: its field is `username`, holding the email.
    const form = new HttpParams({ fromObject: { username: email, password } });
    return this.http.post<void>(apiPath('/api/auth/login'), form);
  }

  logout(): Observable<void> {
    return this.http.post<void>(apiPath('/api/auth/logout'), null);
  }

  /** A new account, by invitation. Only these three: the `is_*` flags are the server's. */
  register(email: string, password: string, inviteCode: string): Observable<UserRead> {
    const body: UserCreate = { email, password, invite_code: inviteCode };
    return this.http.post<UserRead>(apiPath('/api/auth/register'), body);
  }

  /** Where to send the browser to sign in with GitHub. */
  gitHubSignInUrl(): Observable<string> {
    return this.http
      .get<GitHubAuthorize>(apiPath('/api/auth/github/authorize'))
      .pipe(map((response) => response.authorization_url));
  }

  /**
   * Finish a GitHub sign-in: hand the server everything GitHub put on the return address
   * (`code`, `state`). It checks them and sets the session cookie; nothing comes back.
   */
  gitHubSignIn(fromGitHub: Readonly<Record<string, string>>): Observable<void> {
    const params = new HttpParams({ fromObject: fromGitHub });
    return this.http.get<void>(apiPath('/api/auth/github/callback'), { params });
  }
}
