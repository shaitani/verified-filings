import { DatePipe } from '@angular/common';
import { Component, inject, output } from '@angular/core';
import { MatIconModule } from '@angular/material/icon';
import { MatListModule } from '@angular/material/list';
import { RouterLink, RouterLinkActive } from '@angular/router';

import type { ConversationSummary } from '../api/types';
import { HistoryStore } from './history-store';

interface Marker {
  icon: string;
  text: string;
  waiting?: boolean; // the reader has a question to answer
  tone: 'done' | 'partial' | 'busy' | 'waiting' | 'bad'; // the tag's tint
}

/** What a past question's latest round came to, in a word and an icon. */
export function marker(conversation: ConversationSummary): Marker {
  if (conversation.status === 'failed') return { icon: 'error', text: 'failed', tone: 'bad' };
  if (conversation.status !== 'done')
    return { icon: 'hourglass_top', text: 'running', tone: 'busy' };
  switch (conversation.reply_status) {
    case 'answered':
      return { icon: 'check_circle', text: 'answered', tone: 'done' };
    case 'partial':
      return { icon: 'rule', text: 'partly answered', tone: 'partial' };
    case 'asked':
      return { icon: 'help', text: 'waiting for your answer', waiting: true, tone: 'waiting' };
    default:
      return { icon: 'block', text: 'refused', tone: 'bad' };
  }
}

/** "My past questions": each opens its thread. */
@Component({
  selector: 'vf-history-list',
  imports: [DatePipe, MatIconModule, MatListModule, RouterLink, RouterLinkActive],
  template: `
    <h2 class="heading">Past questions</h2>
    @if (history.problem(); as problem) {
      <p class="problem">{{ problem }}</p>
    }
    <mat-nav-list>
      @for (conversation of history.conversations(); track conversation.conversation_id) {
        @let mark = marker(conversation);
        <a
          mat-list-item
          [routerLink]="['/c', conversation.conversation_id]"
          routerLinkActive="current"
          #active="routerLinkActive"
          [activated]="active.isActive"
          (click)="opened.emit()"
        >
          <span matListItemTitle class="question" [title]="conversation.question">{{
            conversation.question
          }}</span>
          <span matListItemLine class="line" [class.waiting]="mark.waiting">
            <span class="status" [attr.data-tone]="mark.tone">
              <mat-icon inline aria-hidden="true" [svgIcon]="mark.icon" />
              {{ mark.text }}
            </span>
            {{ conversation.created_at | date: 'MMM d, y' }}
          </span>
        </a>
      } @empty {
        @if (history.loaded()) {
          <p class="empty">Nothing asked yet.</p>
        }
      }
    </mat-nav-list>
  `,
  styleUrl: './history-list.scss',
})
export class HistoryList {
  protected readonly history = inject(HistoryStore);
  protected readonly marker = marker;

  readonly opened = output(); // a question was picked: a narrow screen closes the drawer
}
