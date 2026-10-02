import { Component, computed, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { MatButtonModule } from '@angular/material/button';
import { MAT_DIALOG_DATA, MatDialogModule, MatDialogRef } from '@angular/material/dialog';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatIconModule } from '@angular/material/icon';
import { MatInputModule } from '@angular/material/input';

/** A yes-or-no before an admin action. `typed`: a word that must be typed to confirm. */
export interface ConfirmData {
  title: string;
  body: string;
  confirm: string; // the button's label
  danger?: boolean;
  typed?: string;
}

/** Closes with `true` when confirmed. */
@Component({
  selector: 'vf-confirm-dialog',
  imports: [FormsModule, MatButtonModule, MatDialogModule, MatFormFieldModule, MatInputModule],
  template: `
    <h2 mat-dialog-title>{{ data.title }}</h2>
    <mat-dialog-content>
      <p>{{ data.body }}</p>
      @if (data.typed; as word) {
        <mat-form-field appearance="outline" class="typed">
          <mat-label>Type {{ word }} to confirm</mat-label>
          <input
            matInput
            autocomplete="off"
            [ngModel]="typed()"
            (ngModelChange)="typed.set($event)"
          />
        </mat-form-field>
      }
    </mat-dialog-content>
    <mat-dialog-actions align="end">
      <button mat-button type="button" mat-dialog-close>Cancel</button>
      <button
        mat-flat-button
        type="button"
        [class.danger]="data.danger"
        [disabled]="!ready()"
        (click)="dialog.close(true)"
      >
        {{ data.confirm }}
      </button>
    </mat-dialog-actions>
  `,
  styles: `
    .typed {
      width: 100%;
    }
    .danger {
      --mat-button-filled-container-color: var(--mat-sys-error);
      --mat-button-filled-label-text-color: var(--mat-sys-on-error);
    }
  `,
})
export class ConfirmDialog {
  protected readonly data = inject<ConfirmData>(MAT_DIALOG_DATA);
  protected readonly dialog = inject(MatDialogRef<ConfirmDialog, boolean>);
  protected readonly typed = signal('');
  protected readonly ready = computed(
    () => !this.data.typed || this.typed().trim() === this.data.typed,
  );
}

/** Something the server shows once -- a new password, an invite code. */
export interface ShownOnceData {
  title: string;
  label: string;
  value: string;
  note: string;
}

@Component({
  selector: 'vf-shown-once-dialog',
  imports: [MatButtonModule, MatDialogModule, MatIconModule],
  template: `
    <h2 mat-dialog-title>{{ data.title }}</h2>
    <mat-dialog-content>
      <p class="label">{{ data.label }}</p>
      <p class="value">
        <code>{{ data.value }}</code>
        <button mat-icon-button type="button" aria-label="Copy" title="Copy" (click)="copy()">
          <mat-icon [svgIcon]="copied() ? 'check' : 'content_copy'" />
        </button>
      </p>
      <p class="note">{{ data.note }} It will not be shown again.</p>
    </mat-dialog-content>
    <mat-dialog-actions align="end">
      <button mat-flat-button type="button" mat-dialog-close>Done</button>
    </mat-dialog-actions>
  `,
  styles: `
    .label {
      margin-bottom: 4px;
      color: var(--mat-sys-on-surface-variant);
    }
    .value {
      display: flex;
      align-items: center;
      gap: 8px;
      margin-top: 0;
    }
    code {
      font-size: 1.25rem;
      letter-spacing: 0.05em;
      user-select: all;
    }
    .note {
      color: var(--mat-sys-on-surface-variant);
    }
  `,
})
export class ShownOnceDialog {
  protected readonly data = inject<ShownOnceData>(MAT_DIALOG_DATA);
  protected readonly copied = signal(false);

  protected async copy(): Promise<void> {
    try {
      await navigator.clipboard.writeText(this.data.value);
      this.copied.set(true);
    } catch {
      // No clipboard (an insecure page): the value is selectable and stays on screen.
    }
  }
}

/** How long a new invite lasts; the server takes 1 to 90 days. Closes with the days. */
@Component({
  selector: 'vf-new-invite-dialog',
  imports: [FormsModule, MatButtonModule, MatDialogModule, MatFormFieldModule, MatInputModule],
  template: `
    <h2 mat-dialog-title>New invite code</h2>
    <mat-dialog-content>
      <p>A single-use code that registers one account, by password or with GitHub.</p>
      <mat-form-field appearance="outline">
        <mat-label>Valid for (days)</mat-label>
        <input
          matInput
          type="number"
          min="1"
          max="90"
          [ngModel]="days()"
          (ngModelChange)="days.set($event)"
        />
      </mat-form-field>
    </mat-dialog-content>
    <mat-dialog-actions align="end">
      <button mat-button type="button" mat-dialog-close>Cancel</button>
      <button mat-flat-button type="button" [disabled]="!valid()" (click)="create()">Create</button>
    </mat-dialog-actions>
  `,
})
export class NewInviteDialog {
  private readonly dialog = inject(MatDialogRef<NewInviteDialog, number>);
  protected readonly days = signal<number>(14);
  protected readonly valid = computed(() => {
    const days = Number(this.days());
    return Number.isInteger(days) && days >= 1 && days <= 90;
  });

  protected create(): void {
    this.dialog.close(Number(this.days()));
  }
}
