import { Component, effect, inject, input } from '@angular/core';

import type { JobStage, RoundView } from '../api/types';
import { ConversationStore } from './conversation-store';
import { StageLine } from './stage-line';

/** `/c/:conversationId` -- one conversation, as its rounds. */
@Component({
  selector: 'vf-thread-page',
  imports: [StageLine],
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
}
