import { HttpErrorResponse } from '@angular/common/http';
import { Component, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { MatButtonModule } from '@angular/material/button';
import { MAT_DIALOG_DATA, MatDialogModule, MatDialogRef } from '@angular/material/dialog';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatInputModule } from '@angular/material/input';

/** What the dialog needs: how to send the report. It closes with `true` once sent. */
export interface ReportData {
  send: (note: string | null) => Promise<void>;
}

/** "Report a problem" on one round, with an optional note for whoever debugs it. */
@Component({
  selector: 'vf-report-dialog',
  imports: [FormsModule, MatButtonModule, MatDialogModule, MatFormFieldModule, MatInputModule],
  template: `
    <h2 mat-dialog-title>Report a problem</h2>
    <mat-dialog-content>
      <p>This round will be flagged for review, with everything it did recorded.</p>
      <mat-form-field appearance="outline" class="note">
        <mat-label>What looks wrong? (optional)</mat-label>
        <textarea matInput rows="4" [(ngModel)]="note"></textarea>
      </mat-form-field>
      @if (problem(); as sentence) {
        <p class="problem" role="alert">{{ sentence }}</p>
      }
    </mat-dialog-content>
    <mat-dialog-actions align="end">
      <button mat-button type="button" mat-dialog-close>Cancel</button>
      <button mat-flat-button type="button" [disabled]="busy()" (click)="send()">Report</button>
    </mat-dialog-actions>
  `,
  styles: `
    .note {
      width: 100%;
    }
    .problem {
      color: var(--mat-sys-error);
    }
  `,
})
export class ReportDialog {
  private readonly data = inject<ReportData>(MAT_DIALOG_DATA);
  private readonly dialog = inject(MatDialogRef<ReportDialog, boolean>);

  protected note = '';
  protected readonly busy = signal(false);
  protected readonly problem = signal<string | null>(null);

  protected async send(): Promise<void> {
    this.busy.set(true);
    this.problem.set(null);
    try {
      await this.data.send(this.note.trim() || null);
      this.dialog.close(true);
    } catch (error) {
      this.problem.set(reportProblem(error));
    } finally {
      this.busy.set(false);
    }
  }
}

/** Why a report could not be sent. The note's length limit is read off the server's refusal. */
export function reportProblem(error: unknown): string {
  if (error instanceof HttpErrorResponse && error.status === 422) {
    const [first] = (error.error?.detail ?? []) as { ctx?: { max_length?: number } }[];
    if (first?.ctx?.max_length) return `A note can be at most ${first.ctx.max_length} characters.`;
  }
  return 'The report could not be sent. Please try again.';
}
