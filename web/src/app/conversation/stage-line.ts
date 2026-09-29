import { Component, DestroyRef, computed, inject, input, signal } from '@angular/core';
import { MatProgressBarModule } from '@angular/material/progress-bar';

import type { JobStage } from '../api/types';

// What each stage is doing, in the reader's words. "Queued" is real: questions run one
// at a time on the one GPU, so a reader may be waiting for someone else's (DESIGN §4).
const DOING: Readonly<Record<JobStage, string>> = {
  queued: 'Waiting for the question before yours to finish',
  parsing: 'Reading the question',
  mapping: 'Matching it to the filings',
  fetching: 'Fetching the figures',
  presenting: 'Preparing the answer',
};

/** A running round: what it is doing, and how long since it was asked -- a running clock. */
@Component({
  selector: 'vf-stage-line',
  imports: [MatProgressBarModule],
  template: `
    <p class="doing" aria-live="polite">
      {{ doing() }}…
      @if (seconds() !== null) {
        <span class="seconds" aria-hidden="true">{{ seconds() }} s</span>
      }
    </p>
    <mat-progress-bar mode="indeterminate" />
  `,
  styles: `
    .doing {
      margin: 0 0 8px;
    }
    .seconds {
      color: var(--mat-sys-on-surface-variant);
      font: var(--mat-sys-body-small);
      margin-left: 8px;
    }
  `,
})
export class StageLine {
  readonly stage = input.required<JobStage>();
  readonly startedAt = input<number | null>(null); // when the round was queued, in ms

  // One tick a second. The seconds are hidden from screen readers so they do not
  // announce every tick; the stage itself is still announced when it changes.
  private readonly now = signal(Date.now());

  constructor() {
    const ticker = setInterval(() => this.now.set(Date.now()), 1000);
    inject(DestroyRef).onDestroy(() => clearInterval(ticker));
  }

  protected readonly doing = computed(() => DOING[this.stage()]);
  protected readonly seconds = computed(() => {
    const started = this.startedAt();
    return started === null ? null : Math.max(0, Math.round((this.now() - started) / 1000));
  });
}
