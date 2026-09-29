import { Component, input } from '@angular/core';
import { MatTableModule } from '@angular/material/table';
import { MatTooltipModule } from '@angular/material/tooltip';

import type { AnswerRow } from '../api/types';

/**
 * Every figure of the answer. It always ships (DESIGN §5): a chart hides the exact
 * figure and has nowhere to hang a citation. Rows come in the Presenter's order.
 */
@Component({
  selector: 'vf-answer-table',
  imports: [MatTableModule, MatTooltipModule],
  template: `
    <table mat-table [dataSource]="rows()">
      <ng-container matColumnDef="metric">
        <th mat-header-cell *matHeaderCellDef>Figure</th>
        <td mat-cell *matCellDef="let row">{{ label(row) }}</td>
      </ng-container>

      <ng-container matColumnDef="company">
        <th mat-header-cell *matHeaderCellDef>Company</th>
        <td mat-cell *matCellDef="let row" [matTooltip]="row.companies.join(', ')">
          {{ row.company }}
        </td>
      </ng-container>

      <ng-container matColumnDef="period">
        <th mat-header-cell *matHeaderCellDef>Period</th>
        <td mat-cell *matCellDef="let row" [matTooltip]="span(row)">
          {{ row.period_label }}
          @if (row.compared_with) {
            <span class="vs">vs {{ row.compared_with }}</span>
          }
        </td>
      </ng-container>

      <ng-container matColumnDef="value">
        <th mat-header-cell *matHeaderCellDef class="number">Value</th>
        <td mat-cell *matCellDef="let row" class="number">{{ row.display }}</td>
      </ng-container>

      <ng-container matColumnDef="sources">
        <th mat-header-cell *matHeaderCellDef>Source</th>
        <td mat-cell *matCellDef="let row" class="sources">
          @for (key of row.citations; track key) {
            <sup>[{{ footnotes().get(key) }}]</sup>
          }
        </td>
      </ng-container>

      <tr mat-header-row *matHeaderRowDef="columns"></tr>
      <tr mat-row *matRowDef="let row; columns: columns"></tr>
    </table>
  `,
  styles: `
    table {
      width: 100%;
    }
    .number {
      text-align: right;
      font-variant-numeric: tabular-nums;
    }
    .vs,
    .sources {
      color: var(--mat-sys-on-surface-variant);
      font: var(--mat-sys-body-small);
    }
    .vs {
      margin-left: 4px;
    }
  `,
})
export class AnswerTable {
  readonly rows = input.required<readonly AnswerRow[]>();
  readonly footnotes = input.required<ReadonlyMap<string, number>>(); // citation key -> [n]

  protected readonly columns = ['metric', 'company', 'period', 'value', 'sources'];

  /** "revenue", or what was computed from it: "revenue growth", "R&D spend average". */
  protected label(row: AnswerRow): string {
    return row.derivation ? `${row.metric} ${row.derivation}` : row.metric;
  }

  /** The dates a figure covers; a balance is a position on one date. */
  protected span(row: AnswerRow): string {
    return row.period_start ? `${row.period_start} → ${row.period_end}` : `as of ${row.period_end}`;
  }
}
