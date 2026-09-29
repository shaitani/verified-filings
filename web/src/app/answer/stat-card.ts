import { Component, input } from '@angular/core';
import { MatCardModule } from '@angular/material/card';

import type { AnswerRow, StatView } from '../api/types';

/** One figure, large: a stat view. Plain Angular -- no chart library needed for a number. */
@Component({
  selector: 'vf-stat-card',
  imports: [MatCardModule],
  template: `
    <mat-card appearance="outlined">
      <mat-card-content>
        <p class="title">{{ view().title }}</p>
        <p class="figure">{{ row().display }}</p>
        <p class="span">
          @if (row().period_start) {
            {{ row().period_start }} → {{ row().period_end }}
          } @else {
            as of {{ row().period_end }}
          }
        </p>
        @if (row().companies.length) {
          <p class="span">across {{ row().companies.join(', ') }}</p>
        }
      </mat-card-content>
    </mat-card>
  `,
  styles: `
    :host {
      display: block;
      max-width: 420px;
    }
    .title {
      font: var(--mat-sys-title-small);
      margin: 0;
    }
    .figure {
      font: var(--mat-sys-display-small);
      margin: 8px 0;
    }
    .span {
      color: var(--mat-sys-on-surface-variant);
      font: var(--mat-sys-body-small);
      margin: 0;
    }
  `,
})
export class StatCard {
  readonly view = input.required<StatView>();
  readonly row = input.required<AnswerRow>(); // rows[view.row]
}
