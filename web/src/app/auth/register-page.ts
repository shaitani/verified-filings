import { Component, inject, signal } from '@angular/core';
import { NonNullableFormBuilder, ReactiveFormsModule, Validators } from '@angular/forms';
import { MatButtonModule } from '@angular/material/button';
import { MatCardModule } from '@angular/material/card';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatInputModule } from '@angular/material/input';
import { Router, RouterLink } from '@angular/router';

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
}
