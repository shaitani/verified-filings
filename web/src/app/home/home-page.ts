import { TextFieldModule } from '@angular/cdk/text-field';
import { HttpErrorResponse } from '@angular/common/http';
import { Component, inject, signal } from '@angular/core';
import { NonNullableFormBuilder, ReactiveFormsModule, Validators } from '@angular/forms';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';
import { MatInputModule } from '@angular/material/input';
import { Router } from '@angular/router';
import { firstValueFrom } from 'rxjs';

import { ApiService } from '../api/api.service';
import { limitProblem } from '../api/limits';

/** `/` -- ask a new question. */
@Component({
  selector: 'vf-home-page',
  imports: [MatButtonModule, MatIconModule, MatInputModule, ReactiveFormsModule, TextFieldModule],
  templateUrl: './home-page.html',
  styleUrl: './home-page.scss',
})
export class HomePage {
  private readonly api = inject(ApiService);
  private readonly router = inject(Router);

  protected readonly form = inject(NonNullableFormBuilder).group({
    question: ['', Validators.required],
  });
  protected readonly busy = signal(false);
  protected readonly problem = signal<string | null>(null);

  protected async ask(): Promise<void> {
    const question = this.form.getRawValue().question.trim();
    if (!question) return;
    this.busy.set(true);
    this.problem.set(null);
    try {
      const created = await firstValueFrom(this.api.ask(question));
      await this.router.navigate(['/c', created.conversation_id]); // the thread follows the round
    } catch (error) {
      this.problem.set(askProblem(error));
    } finally {
      this.busy.set(false);
    }
  }

  /** Enter asks; Shift+Enter starts a new line. */
  protected onEnter(event: Event): void {
    if ((event as KeyboardEvent).shiftKey) return;
    event.preventDefault();
    void this.ask();
  }
}

/** Why a question could not be asked. The length limit is read off the server's refusal. */
export function askProblem(error: unknown): string {
  const limited = limitProblem(error);
  if (limited) return limited;
  if (error instanceof HttpErrorResponse && error.status === 422) {
    const [first] = (error.error?.detail ?? []) as {
      type?: string;
      ctx?: { max_length?: number };
    }[];
    if (first?.type === 'string_too_long' && first.ctx?.max_length) {
      return `A question can be at most ${first.ctx.max_length} characters.`;
    }
  }
  if (error instanceof HttpErrorResponse && (error.status === 0 || error.status >= 500)) {
    return 'The server could not be reached. Please try again in a moment.';
  }
  return 'That question could not be asked. Please try again.';
}
