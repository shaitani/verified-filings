import { Component, effect, inject, input } from '@angular/core';

import type { AnswerIn, ConversationView, JobStage, RoundView } from '../api/types';
import { ConversationStore } from './conversation-store';
import { ReplyView } from './reply-view';
import { StageLine } from './stage-line';

/** `/c/:conversationId` -- one conversation, as its rounds. */
@Component({
  selector: 'vf-thread-page',
  imports: [ReplyView, StageLine],
  providers: [ConversationStore], // one store per open thread, gone when the page is
  templateUrl: './thread-page.html',
  styleUrl: './thread-page.scss',
})
export class ThreadPage {
  protected readonly store = inject(ConversationStore);

  readonly conversationId = input.required<string>(); // from the route

  constructor() {
    effect(() => void this.store.open(this.conversationId()));
  }

  /** What the reader picked to start this round, in words. */
  protected picks(round: RoundView): string {
    return round.answers.map((answer) => answer.text).join(', ');
  }

  /** The live stage if it is this round's, else the stage the database last recorded. */
  protected stageOf(round: RoundView): JobStage {
    const live = this.store.stage();
    return live?.stage ?? (round.status as JobStage);
  }

  /** Only the last round's questions are open, and only once nothing is running. */
  protected answerable(conversation: ConversationView, index: number): boolean {
    return index === conversation.rounds.length - 1 && this.store.running() === null;
  }

  /** The picks the next round was started with: they answered this round's questions. */
  protected chosen(conversation: ConversationView, index: number): ReadonlyMap<string, string> {
    const next = conversation.rounds[index + 1];
    return new Map(
      (next?.answers ?? []).flatMap((a) => (a.option_id ? [[a.ask_id, a.option_id] as const] : [])),
    );
  }

  protected answer(picks: readonly AnswerIn[]): void {
    void this.store.answer(picks);
  }
}
