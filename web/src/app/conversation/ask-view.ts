import { Component, input, output } from '@angular/core';
import { MatRadioModule } from '@angular/material/radio';
import { MatTooltipModule } from '@angular/material/tooltip';

import type { Ask } from '../api/types';

/** One question back, with its options (DESIGN §3). */
@Component({
  selector: 'vf-ask-view',
  imports: [MatRadioModule, MatTooltipModule],
  template: `
    <p class="question">
      {{ ask().question }}
      @if (ask().kind === 'ambiguity') {
        <!-- The machine could not choose between raw concepts: worth seeing, and worth a
             curated alias entry (semantic DESIGN §3). -->
        <span
          class="tag"
          matTooltip="The match was not certain: these are the closest figures in the filings."
          >ambiguous</span
        >
      }
    </p>
    <mat-radio-group
      [attr.aria-label]="ask().question"
      [value]="picked()"
      [disabled]="!answerable()"
      (change)="picks.emit($event.value)"
    >
      @for (option of ask().options; track option.option_id) {
        <mat-radio-button [value]="option.option_id">
          <span class="label">{{ option.label }}</span>
          <span class="description">{{ option.description }}</span>
        </mat-radio-button>
      }
    </mat-radio-group>
  `,
  styleUrl: './ask-view.scss',
})
export class AskView {
  readonly ask = input.required<Ask>();
  readonly picked = input<string | null>(null); // option_id: this round's pick, or the one made
  readonly answerable = input(false); // only the latest round's questions can be answered

  readonly picks = output<string>(); // an option_id
}
