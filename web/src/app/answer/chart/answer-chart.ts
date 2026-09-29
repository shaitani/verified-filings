import { Component, computed, input } from '@angular/core';
import { ChartComponent } from 'ng-apexcharts';

import type { AnswerRow, BarView, LineView } from '../../api/types';
import { chartOptions } from './chart-options';

/**
 * The one component that draws a chart. It takes a view and the rows it points into; the
 * rest of the client never sees the chart library (enforced in eslint.config.js).
 */
@Component({
  selector: 'vf-answer-chart',
  imports: [ChartComponent],
  template: `
    <h3 class="title">{{ view().title }}</h3>
    <apx-chart
      [chart]="options().chart"
      [series]="options().series"
      [xaxis]="options().xaxis"
      [yaxis]="options().yaxis"
      [tooltip]="options().tooltip"
      [dataLabels]="options().dataLabels"
      [legend]="options().legend"
      [stroke]="options().stroke"
      [markers]="options().markers"
    />
  `,
  styles: `
    .title {
      font: var(--mat-sys-title-small);
      margin: 0 0 4px;
    }
  `,
})
export class AnswerChart {
  readonly view = input.required<LineView | BarView>();
  readonly rows = input.required<readonly AnswerRow[]>();

  protected readonly options = computed(() => chartOptions(this.view(), this.rows()));
}
