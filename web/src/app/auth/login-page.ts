import { Component, inject, input, signal } from '@angular/core';
import { NonNullableFormBuilder, ReactiveFormsModule, Validators } from '@angular/forms';
import { MatButtonModule } from '@angular/material/button';
import { MatCardModule } from '@angular/material/card';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatInputModule } from '@angular/material/input';
import { Router, RouterLink } from '@angular/router';
import { firstValueFrom } from 'rxjs';

import { ApiService } from '../api/api.service';
import { signInProblem } from './auth-errors';
import { safeReturnTo } from './auth-guards';
import { AuthStore } from './auth-store';

@Component({
  selector: 'vf-login-page',
  imports: [
    MatButtonModule,
    MatCardModule,
    MatFormFieldModule,
    MatInputModule,
    ReactiveFormsModule,
    RouterLink,
  ],
  templateUrl: './login-page.html',
  styleUrl: './sign-in-pages.scss',
})
export class LoginPage {
  private readonly auth = inject(AuthStore);
  private readonly api = inject(ApiService);
  private readonly router = inject(Router);

  readonly returnTo = input<string>(); // ?returnTo=, bound by the router

  protected readonly form = inject(NonNullableFormBuilder).group({
    email: ['', [Validators.required, Validators.email]],
    password: ['', Validators.required],
  });
  protected readonly busy = signal(false);
  protected readonly problem = signal<string | null>(null);

  protected async submit(): Promise<void> {
    if (this.form.invalid) return;
    await this.attempt('login', async () => {
      const { email, password } = this.form.getRawValue();
      await this.auth.signIn(email, password);
      await this.router.navigateByUrl(safeReturnTo(this.returnTo()));
    });
  }

  protected async withGitHub(): Promise<void> {
    await this.attempt('github', async () => {
      // Leaves this app: GitHub returns the browser to /auth/github/callback.
      window.location.assign(await firstValueFrom(this.api.gitHubSignInUrl()));
    });
  }

  private async attempt(step: 'login' | 'github', work: () => Promise<void>): Promise<void> {
    this.busy.set(true);
    this.problem.set(null);
    try {
      await work();
    } catch (error) {
      this.problem.set(signInProblem(error, step));
    } finally {
      this.busy.set(false);
    }
  }
}
