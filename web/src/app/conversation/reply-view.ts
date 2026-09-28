import { Component, computed, input, output, signal } from '@angular/core';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';

import type { AnswerIn, AnswerRow, Ask, Part, Reply } from '../api/types';
import { AskView } from './ask-view';

/** One round's reply: a refusal that sank the question, or each part and what became of it. */
@Component({
  selector: 'vf-reply-view',
  imports: [AskView, MatButtonModule, MatIconModule],
  templateUrl: './reply-view.html',
  styleUrl: './reply-view.scss',
})
export class ReplyView {
  readonly reply = input.required<Reply>();
  readonly answerable = input(false); // the latest round, finished, nothing running
  readonly chosen = input<ReadonlyMap<string, string>>(new Map()); // ask_id -> option_id, picked earlier
  readonly busy = input(false); // an answer is on its way

  readonly answered = output<readonly AnswerIn[]>();

  private readonly picking = signal<ReadonlyMap<string, string>>(new Map());

  protected readonly asks = computed(() =>
    this.reply().parts.flatMap((part) => (part.ask ? [part.ask] : [])),
  );

  /** Every question has a pick: the reader cannot send half an answer by accident. */
  protected readonly complete = computed(
    () => this.asks().length > 0 && this.asks().every((ask) => this.picking().has(ask.ask_id)),
  );

  protected pickedFor(ask: Ask): string | null {
    return this.picking().get(ask.ask_id) ?? this.chosen().get(ask.ask_id) ?? null;
  }

  protected pick(ask: Ask, optionId: string): void {
    this.picking.update((picks) => new Map(picks).set(ask.ask_id, optionId));
  }

  protected send(): void {
    this.answered.emit(
      [...this.picking()].map(([ask_id, option_id]) => ({ kind: 'option', ask_id, option_id })),
    );
  }

  // Until slice 6 draws the answer: each figure of an answered part, as the Presenter wrote it.
  protected rowsFor(part: Part): readonly AnswerRow[] {
    return this.reply().answer?.rows.filter((row) => row.element_id === part.part_id) ?? [];
  }
}
