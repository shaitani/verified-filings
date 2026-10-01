import { HttpErrorResponse } from '@angular/common/http';
import { Component, inject, signal } from '@angular/core';
import { NonNullableFormBuilder, ReactiveFormsModule, Validators } from '@angular/forms';
import { MatButtonModule } from '@angular/material/button';
import { MatCardModule } from '@angular/material/card';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatInputModule } from '@angular/material/input';
import { Router, RouterLink } from '@angular/router';
import { firstValueFrom } from 'rxjs';

import { ApiService } from '../api/api.service';
import { signInProblem } from './auth-errors';
import { AuthStore } from './auth-store';

@Component({
  selector: 'vf-register-page',
  imports: [
    MatButtonModule,
    MatCardModule,
    MatFormFieldModule,
    MatInputModule,
    ReactiveFormsModule,
    RouterLink,
  ],
  templateUrl: './register-page.html',
  styleUrl: './sign-in-pages.scss',
})
export class RegisterPage {
  private readonly auth = inject(AuthStore);
  private readonly api = inject(ApiService);
  private readonly router = inject(Router);

  // Only presence is checked here. Password length and the invite are the server's
  // rules, and its sentences say what is wrong, so the two cannot disagree.
  protected readonly form = inject(NonNullableFormBuilder).group({
    email: ['', [Validators.required, Validators.email]],
    password: ['', Validators.required],
    inviteCode: ['', Validators.required],
  });
  protected readonly busy = signal(false);
  protected readonly problem = signal<string | null>(null);

  protected async submit(): Promise<void> {
    if (this.form.invalid) return;
    this.busy.set(true);
    this.problem.set(null);
    try {
      const { email, password, inviteCode } = this.form.getRawValue();
      await this.auth.register(email, password, inviteCode.trim());
      await this.router.navigateByUrl('/');
    } catch (error) {
      this.problem.set(signInProblem(error, 'register'));
    } finally {
      this.busy.set(false);
    }
  }

  /**
   * Sign up through GitHub: the invite code goes to the server first, which checks it
   * and holds it for when GitHub sends the browser back -- so a wrong code is said
   * here, not after the trip. Email and password are not needed: GitHub supplies both.
   */
  protected async withGitHub(): Promise<void> {
    const inviteCode = this.form.controls.inviteCode.value.trim();
    if (!inviteCode) {
      this.problem.set('Enter your invite code first: signing up with GitHub needs it too.');
      return;
    }
    this.busy.set(true);
    this.problem.set(null);
    try {
      await firstValueFrom(this.api.holdGitHubInvite(inviteCode));
      // Leaves this app: GitHub returns the browser to /auth/github/callback.
      window.location.assign(await firstValueFrom(this.api.gitHubSignInUrl()));
    } catch (error) {
      // 404: GitHub is not set up on this server. Anything else: the code's fault.
      const gitHubMissing = error instanceof HttpErrorResponse && error.status === 404;
      this.problem.set(signInProblem(error, gitHubMissing ? 'github' : 'register'));
    } finally {
      this.busy.set(false);
    }
  }
}
