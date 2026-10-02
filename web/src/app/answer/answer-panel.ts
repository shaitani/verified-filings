import { Component, computed, input } from '@angular/core';
import { MatExpansionModule } from '@angular/material/expansion';
import { MatIconModule } from '@angular/material/icon';

import type { AnswerView, BarView, LineView, StatView, View } from '../api/types';
import { AnswerTable } from './answer-table';
import { AnswerChart } from './chart/answer-chart';
import { CitationsList } from './citations-list';
import { StatCard } from './stat-card';

/**
 * The figures of a reply, in the order DESIGN §5 and §6 set: notes and conditions first
 * (they are part of the answer), then the views, then the table, which always ships,
 * then what it was answered as. Every figure's text is the Presenter's.
 */
@Component({
  selector: 'vf-answer-panel',
  imports: [AnswerChart, AnswerTable, CitationsList, MatExpansionModule, MatIconModule, StatCard],
  template: `
    @for (note of answer().notes; track $index) {
      <p class="note" [attr.data-kind]="note.kind">
        <mat-icon aria-hidden="true" svgIcon="info" />
        <span class="message">{{ note.message }}</span>
      </p>
    }
    @for (condition of answer().conditions; track $index) {
      <p class="condition">Only where: {{ condition }}</p>
    }

    <div class="views">
      @for (view of answer().views; track $index) {
        @if (asStat(view); as stat) {
          <vf-stat-card [view]="stat" [row]="answer().rows[stat.row]" />
        } @else if (asChart(view); as chart) {
          <vf-answer-chart class="chart" [view]="chart" [rows]="answer().rows" />
        }
      }
    </div>

    <!-- The table always ships (DESIGN §5); it starts open, and the reader can fold it away. -->
    <mat-expansion-panel expanded>
      <mat-expansion-panel-header>
        <mat-panel-title>Source figures</mat-panel-title>
        <mat-panel-description>{{ answer().rows.length }} figure(s)</mat-panel-description>
      </mat-expansion-panel-header>
      <vf-answer-table [rows]="answer().rows" [footnotes]="footnotes()" />
    </mat-expansion-panel>
    <vf-citations-list
      [citations]="answer().citations"
      [rows]="answer().rows"
      [footnotes]="footnotes()"
    />
  `,
  styleUrl: './answer-panel.scss',
})
export class AnswerPanel {
  readonly answer = input.required<AnswerView>();

  /** Each citation's footnote number, in the order the table first cites it. */
  protected readonly footnotes = computed(() => {
    const numbers = new Map<string, number>();
    for (const row of this.answer().rows) {
      for (const key of row.citations) if (!numbers.has(key)) numbers.set(key, numbers.size + 1);
    }
    return numbers;
  });

  protected asStat(view: View): StatView | null {
    return view.kind === 'stat' ? view : null;
  }

  protected asChart(view: View): LineView | BarView | null {
    return view.kind === 'stat' ? null : view;
  }
}
