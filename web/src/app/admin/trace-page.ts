import { DatePipe, DecimalPipe } from '@angular/common';
import { HttpErrorResponse } from '@angular/common/http';
import { Component, computed, effect, inject, input, signal } from '@angular/core';
import { MatButtonModule } from '@angular/material/button';
import { MatExpansionModule } from '@angular/material/expansion';
import { MatIconModule } from '@angular/material/icon';
import { RouterLink } from '@angular/router';
import { firstValueFrom } from 'rxjs';

import { AdminApi } from './admin-api';
import type { AdminTrace } from '../api/types';
import { adminProblem } from './admin-store';

/** One model call, as app/trace.py records it. */
export interface ModelCall {
  stage: string;
  model: string;
  prompt: string;
  reply: string | null;
  seconds: number;
  error: string | null;
}

/** One SQL statement: what the validator or the database made of it. */
export interface Statement {
  sql: string;
  verdict: unknown;
  error: string | null;
}

/** An exception a stage raised (app/chain.py ErrorRecord). */
export interface StageError {
  stage: string;
  type: string;
  message: string;
  traceback: string;
}

function list<T>(value: unknown): T[] {
  return Array.isArray(value) ? (value as T[]) : [];
}

function entries(value: unknown): [string, unknown][] {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? Object.entries(value as Record<string, unknown>)
    : [];
}

/**
 * `/admin/rounds/:jobId` -- one round's whole record (DESIGN §8, §13): what was reported,
 * what broke, how long each stage took, every model call and statement, and the three
 * contract objects. For a person debugging; opening it is written to the audit log.
 */
@Component({
  selector: 'vf-trace-page',
  imports: [DatePipe, DecimalPipe, MatButtonModule, MatExpansionModule, MatIconModule, RouterLink],
  templateUrl: './trace-page.html',
  styleUrl: './trace-page.scss',
})
export class TracePage {
  private readonly api = inject(AdminApi);

  readonly jobId = input.required<string>(); // :jobId, bound by the router

  protected readonly trace = signal<AdminTrace | null>(null);
  protected readonly problem = signal<string | null>(null);
  protected readonly copied = signal<string | null>(null); // which copy button last worked

  protected readonly modelCalls = computed(() => list<ModelCall>(this.trace()?.model_calls));
  protected readonly statements = computed(() => list<Statement>(this.trace()?.statements));
  protected readonly errors = computed(() => list<StageError>(this.trace()?.errors));
  protected readonly timings = computed(() =>
    entries(this.trace()?.timings).map(([stage, seconds]) => ({ stage, seconds: Number(seconds) })),
  );
  protected readonly models = computed(() =>
    entries(this.trace()?.models).map(([role, model]) => `${role}: ${String(model)}`),
  );
  protected readonly totalSeconds = computed(() =>
    this.timings().reduce(
      (sum, timing) => sum + (Number.isFinite(timing.seconds) ? timing.seconds : 0),
      0,
    ),
  );
  /** A trace written by code with uncommitted changes (app/api/trace.py marks it). */
  protected readonly dirty = computed(() => this.trace()?.code_version?.includes('dirty') ?? false);

  constructor() {
    effect(() => void this.load(this.jobId()));
  }

  private async load(jobId: string): Promise<void> {
    this.trace.set(null);
    this.problem.set(null);
    try {
      this.trace.set(await firstValueFrom(this.api.trace(jobId)));
    } catch (error) {
      const gone = error instanceof HttpErrorResponse && error.error?.detail === 'NOT_FOUND';
      this.problem.set(gone ? 'There is no such round.' : adminProblem(error));
    }
  }

  protected json(value: unknown): string {
    return JSON.stringify(value, null, 2);
  }

  /** Copy text; `key` names the button, so it alone shows the tick. */
  protected async copy(key: string, text: string): Promise<void> {
    try {
      await navigator.clipboard.writeText(text);
      this.copied.set(key);
    } catch {
      // No clipboard (an insecure page): the text is on screen and selectable.
    }
  }

  /** The whole record as JSON: one paste hands a round over for debugging. */
  protected copyAll(): Promise<void> {
    return this.copy('all', this.json(this.trace()));
  }
}
